// ===== Dateien-Ansicht: hochladen, entpacken, indexieren =====
// Ziel ist C:\Argus_Workspace\Uploads -- genau der Ordner, den der Agent lesen
// darf. Alle Grenzen (Typ, Groesse) sitzen im Backend; hier stehen sie nur
// nochmals, damit man sie SIEHT, statt sie als kommentarlose Ablehnung zu erleben.
(function () {
  let bound = false;
  // Die zuletzt hochgeladenen Dateien (Name und Groesse) -- daran haengen die
  // zwei Ziele und die Warteliste im Dialog.
  let offen = [];
  let konfig = null;

  function log(title, text) {
    const box = $('files-log');
    if (!box) return;
    box.hidden = false;
    $('files-log-body').textContent = title + '\n\n' + text;
  }

  // XMLHttpRequest statt fetch: nur darueber gibt es einen Fortschritt beim
  // SENDEN. 401 landet hier genauso wie in api() bei der Anmeldung.
  function upload(file, onProgress) {
    return new Promise(function (resolve, reject) {
      const form = new FormData();
      form.append('file', file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/v1/dashboard/uploads');
      xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable) onProgress(ev.loaded / ev.total);
      };
      xhr.onload = function () {
        if (xhr.status === 401) {
          if (window.argusAuth) window.argusAuth.sessionLost();
          reject(new Error('Nicht angemeldet'));
          return;
        }
        let payload = {};
        try { payload = JSON.parse(xhr.responseText); } catch (e) { /* kein JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
        else reject(new Error(payload.detail || ('HTTP ' + xhr.status)));
      };
      xhr.onerror = function () { reject(new Error('Verbindung abgebrochen')); };
      xhr.send(form);
    });
  }

  async function queueFiles(list) {
    const queue = $('upload-queue');
    const files = Array.prototype.slice.call(list || []);
    const geschafft = [];
    for (const file of files) {
      const item = document.createElement('div');
      item.className = 'upload-item';
      item.innerHTML = "<span class='upload-item-name'>" + escapeHtml(file.name) + "</span>" +
        "<span class='upload-item-state'>" + formatBytes(file.size) + "</span>" +
        "<div class='progress-container'><div class='progress-fill'></div></div>";
      queue.appendChild(item);
      const bar = item.querySelector('.progress-fill');
      const state = item.querySelector('.upload-item-state');
      try {
        const res = await upload(file, function (p) { bar.style.width = Math.round(p * 100) + '%'; });
        bar.style.width = '100%';
        item.classList.add('is-ok');
        state.textContent = 'hochgeladen';
        geschafft.push({ name: (res && res.name) || file.name,
                         size: (res && res.size) || file.size });
      } catch (e) {
        item.classList.add('is-error');
        state.textContent = (e && e.message) || 'fehlgeschlagen';
      }
      setTimeout(function () { item.remove(); }, 6000);
    }
    // Erst jetzt die Frage stellen: was soll mit dem Hochgeladenen passieren?
    zieleZeigen(geschafft);
  }

  // Nach dem Hochladen zwei Wege: als Notiz in den Chat (Argus erfaehrt davon und
  // kann nachfragen) oder direkt in den Index. Beides ist moeglich -- nacheinander.
  function zieleZeigen(dateien) {
    const box = $('upload-ziele');
    if (!box) return;
    offen = (dateien || []).slice();
    box.hidden = !offen.length;
    listeZeichnen();
    ['ziel-chat', 'ziel-ingest'].forEach(function (id) {
      const b = $(id); if (b) b.disabled = false;
    });
  }

  // Eine Zeile je wartender Datei. Das x nimmt sie aus der AUSWAHL, nicht von
  // der Platte -- geloescht wird im Monitor unter Collection.
  function listeZeichnen() {
    const liste = $('upload-liste');
    if (!liste) return;
    liste.innerHTML = offen.map(function (d, i) {
      return "<div class='upload-zeile'>"
        + "<i>" + (window.argusIcon ? window.argusIcon('datei', 14) : '') + "</i>"
        + "<span class='upload-zeile-name' title='" + escapeHtml(d.name) + "'>"
        + escapeHtml(d.name) + "</span>"
        + "<span class='upload-zeile-groesse'>" + formatBytes(d.size || 0) + "</span>"
        + "<span class='upload-zeile-zustand'>BEREIT</span>"
        + "<button class='upload-zeile-weg' type='button' data-weg='" + i
        + "' title='Aus Auswahl nehmen'>" + (window.argusIcon ? window.argusIcon('x', 13) : '&times;')
        + "</button></div>";
    }).join('');
    liste.querySelectorAll('[data-weg]').forEach(function (b) {
      b.addEventListener('click', function () {
        offen.splice(Number(b.dataset.weg), 1);
        $('upload-ziele').hidden = !offen.length;
        listeZeichnen();
      });
    });
    fussZeichnen();
  }

  // Womit indexiert wird, steht da, BEVOR man auf Ingestieren drueckt.
  function fussZeichnen() {
    const text = $('upload-ziele-text');
    if (!text) return;
    const c = konfig || {};
    const teile = [offen.length + (offen.length === 1 ? ' DATEI' : ' DATEIEN')];
    if (c.embedding_model) {
      teile.push('INGEST MIT ' + c.embedding_model.replace('BAAI/', '').toUpperCase());
    }
    if (c.chunk_size) teile.push('CHUNK ' + c.chunk_size + '/' + (c.chunk_overlap || 0));
    text.textContent = teile.join(' \u00b7 ');
  }

  async function zielChat() {
    if (!offen.length) return;
    const namen = offen.map(function (d) { return d.name; });
    if (window.argusChat && window.argusChat.noteUpload) window.argusChat.noteUpload(namen);
    const b = $('ziel-chat'); if (b) b.disabled = true;
    close();
  }

  async function zielIngest() {
    if (!offen.length) return;
    const b = $('ziel-ingest');
    if (b) { b.disabled = true; b.textContent = 'indexiere \u2026'; }
    const zeilen = [];
    for (const datei of offen) {
      const name = datei.name;
      try {
        const res = await apiPost('/v1/dashboard/uploads/' + encodeURIComponent(name) + '/ingest');
        (res.results || []).forEach(function (r) {
          zeilen.push(name + ': ' + (r.status === 'ok' ? (r.chunks || 0) + ' Chunks' : (r.error || r.status)));
        });
        if (!(res.results || []).length) zeilen.push(name + ': ' + (res.status || 'ok'));
      } catch (e) {
        zeilen.push(name + ': ' + ((e && e.message) || 'fehlgeschlagen'));
      }
    }
    if (b) b.textContent = 'Ingestieren';
    log('Indexiert', zeilen.join('\n'));
  }

  function open() {
    const modal = $('files-modal');
    if (!modal) return;
    bind();
    modal.hidden = false;
    document.body.classList.add('modal-open');
    const ziele = $('upload-ziele');
    if (ziele) ziele.hidden = true;
    offen = [];
    listeZeichnen();
    const zone = $('dropzone-icon');
    if (zone && !zone.innerHTML && window.argusIcon) zone.innerHTML = window.argusIcon('hoch', 26);
    //--- Einmal je Sitzung: Chunk-Werte und Embedding fuer die Fusszeile.
    if (!konfig) {
      api('/v1/dashboard/config').then(function (r) {
        konfig = r.config || {};
        const hinweis = $('dropzone-hint');
        if (hinweis && konfig.max_upload_mb) {
          hinweis.innerHTML = hinweis.innerHTML + ' \u00b7 MAX ' + konfig.max_upload_mb + ' MB';
        }
        fussZeichnen();
      }).catch(function () { /* Fusszeile ist Beiwerk */ });
    }
  }

  function close() {
    const modal = $('files-modal');
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove('modal-open');
  }

  function bind() {
    if (bound) return;
    const zone = $('dropzone'), input = $('files-input');
    if (!zone || !input) return;
    bound = true;

    $('files-modal-close').addEventListener('click', close);
    const zc = $('ziel-chat'), zi = $('ziel-ingest');
    if (zc) zc.addEventListener('click', zielChat);
    if (zi) zi.addEventListener('click', zielIngest);
    $('files-modal-backdrop').addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('files-modal').hidden) close();
    });

    zone.addEventListener('click', function () { input.click(); });
    const durch = $('dropzone-btn');
    //--- stopPropagation: sonst zaehlt der Klick auch als Klick auf die Zone und
    //--- der Dateidialog geht zweimal auf.
    if (durch) durch.addEventListener('click', function (e) { e.stopPropagation(); input.click(); });
    zone.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () {
      queueFiles(input.files);
      input.value = '';
    });
    ['dragenter', 'dragover'].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.add('is-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.remove('is-over');
      });
    });
    zone.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) queueFiles(e.dataTransfer.files);
    });
    $('files-log-close').addEventListener('click', function () { $('files-log').hidden = true; });
  }

  // uploadOne: derselbe Upload ohne Modal-Oberflaeche -- chat.js benutzt ihn fuer
  // gezogene Dateien, damit es die XHR-Logik nicht ein zweites Mal gibt.
  window.argusFiles = { open: open, close: close, uploadOne: upload };
})();
