function getStatusLabel(status) {
  if (!status) return "-";
  const s = status.toLowerCase();
  if (s === "ok" || s === "success") return "ERFOLGREICH";
  if (s === "error") return "FEHLER";
  if (s === "exception") return "SYSTEMAUSNAHME";
  if (s === "blocked") return "BLOCKIERT";
  if (s === "dry_run_native_blocked") return "PRÄVENTIV BLOCKIERT";
  if (s === "dry_run_failed") return "TESTLAUF FEHLGESCHLAGEN";
  if (s === "confirmation_no_channel") return "KEIN FREIGABE-KANAL";
  if (s === "confirmation_timeout") return "BESTÄTIGUNG ZEITÜBERSCHREITUNG";
  if (s === "confirmation_rejected") return "ABGELEHNT";
  if (s === "confirmation_approved") return "FREIGEGEBEN";
  return status.toUpperCase().replace(/_/g, " ");
}

function getFriendlyActionName(tool, details) {
  // String(): ein Audit-Eintrag ohne Werkzeugnamen wirft sonst mitten im tick()
  // einen TypeError, den der stille catch verschluckt.
  const name = String(tool || "");
  if (!details) return name;
  if (name.toLowerCase().includes("powershell")) {
    const clean = details.trim().replace(/^#.*$/gm, "").trim();
    const firstLine = clean.split('\n')[0] || "";
    if (firstLine.length > 40) {
      return firstLine.substring(0, 37) + "...";
    }
    return firstLine || "PowerShell Befehl";
  }
  if (details.startsWith("{")) {
    try {
      const p = JSON.parse(details);
      if (p.path) return `${name}: ${p.path.split('/').pop().split('\\').pop()}`;
      if (p.query) return `${name}: "${p.query}"`;
    } catch(e) {}
  }
  return name;
}

window.toggleAuditRow = function(rowEl) {
  const nextRow = rowEl.nextElementSibling;
  if (nextRow && nextRow.classList.contains("audit-details-row")) {
    const isHidden = nextRow.style.display === "none";
    nextRow.style.display = isHidden ? "table-row" : "none";
    rowEl.querySelector(".expand-chevron").classList.toggle("is-auf", isHidden);
  }
};

window.toggleMissionRow = async function (rowEl, missionId) {
  const nextRow = rowEl.nextElementSibling;
  if (!nextRow || !nextRow.classList.contains("mission-details-row")) return;
  const isHidden = nextRow.style.display === "none";
  nextRow.style.display = isHidden ? "table-row" : "none";
  const chev = rowEl.querySelector(".expand-chevron");
  if (chev) chev.classList.toggle("is-auf", isHidden);
  if (!isHidden) return;
  const pre = nextRow.querySelector(".mission-result-pre");
  try {
    const d = await api("/v1/dashboard/missions/" + missionId);
    let text = "Ziel: " + (d.goal || "-") + "\n";
    if (d.error) text += "\nFehler: " + d.error + "\n";
    text += "\n" + (d.result || "(noch kein Ergebnis)");
    if (pre) pre.textContent = text;
  } catch (e) { if (pre) pre.textContent = "Fehler beim Laden des Ergebnisses."; }
};

let tickNum = 0;

//--- Der erste Aufruf ist die Lebendprobe. Ein Fehler beim Nachladen einzelner
//--- Panels sagt ueber die Verbindung nichts aus -- daher zwei getrennte
//--- try-Bloecke.
let failStreak = 0;
const OFFLINE_AFTER = 2;   // Erst der zweite Fehlschlag zaehlt. Ein einzelner Aussetzer
                           // soll die Anzeige nicht flackern lassen.

function setConnection(online) {
  const pill = $("nav-status");
  const label = $("nav-status-text");
  const ops = $("view-monitor");
  if (pill) pill.classList.toggle("is-offline", !online);
  if (label) label.textContent = online ? "ONLINE" : "OFFLINE";
  // Zahlen bleiben stehen, aber ausgegraut: eingefroren ist nicht aktuell.
  if (ops) ops.classList.toggle("is-stale", !online);
}

function noteConnection(ok) {
  if (ok) {
    failStreak = 0;
    setConnection(true);
  } else if (++failStreak >= OFFLINE_AFTER) {
    setConnection(false);
  }
}

// force=true zieht auch die schweren Panels (Evals/Audit/Missionen), die sonst nur
// jeden 5. Tick laufen -- sonst stuende eine geloeschte Zeile noch 15s in der Tabelle.
async function tick(force) {
  let s;
  try {
    s = await api("/v1/dashboard/summary");
  } catch (e) {
    noteConnection(false);
    tickNum++;
    return;
  }
  noteConnection(true);

  try {
    const eng = s.engine || {};
    const be = s.backend || {};
    const tts = s.tts || {};

    // Echte Konfig-Werte + Live-Status statt Platzhalter
    systemPanel(s);
    
    // Ring 1: LLM Throughput (Outer, circumference 534). Max 100 tok/s.
    const genSpeed = eng.gen_throughput_tok_s !== undefined ? eng.gen_throughput_tok_s : be.avg_tok_s_backend;
    const tokenSpeed = genSpeed !== undefined && genSpeed !== null ? genSpeed : 0;
    window.currentLlmThroughput = tokenSpeed;
    
    // TTFT, Gesamtdauer, Warteschlange und Prefix-Cache stehen in den Kacheln.
    const kvPct = eng.kv_cache_usage !== undefined ? Math.round(eng.kv_cache_usage * 100) : 0;
    window.currentKvCache = kvPct / 100;
    navKv(kvPct, be, eng.kv_pool_tokens || 0);

    // Ohne diese Zeile ist ein niedriger KV-Wert mehrdeutig: wenig Kontext oder
    // gerade verdichtet?
    const compTotal = be.compactions_total || 0;
    // Zahl und Zeitpunkt der Verdichtung stehen in der KV-Kachel und in der Kopfzeile.
    // Der Orb pulst kurz, wenn seit dem letzten Tick eine Kompaktierung dazukam.
    if (window.lastCompactionCount !== undefined && compTotal > window.lastCompactionCount) {
      window.kvCompactFlashUntil = Date.now() + 6000;
    }
    window.lastCompactionCount = compTotal;
    
    // Sprachausgabe: Latenz und RTF stehen in der Prefix-Cache-Kachel. Die
    // Zeichenrate treibt weiterhin den Orb.
    const ttsSpeed = tts.avg_chars_per_sec !== undefined && tts.avg_chars_per_sec !== null ? tts.avg_chars_per_sec : 0;
    window.currentTtsThroughput = ttsSpeed;
    
    // Sparkline
    const recentReqs = be.recent || [];
    const throughputHistory = recentReqs.map(r => r.tok_s !== null && r.tok_s !== undefined ? r.tok_s : 0);
    kacheln(be, eng, tts, kvPct, throughputHistory);
    
    // Qdrant stats
    // Active Agent Loads
    const totalRoutes = Object.values(be.routes || {}).reduce((a,b)=>a+b, 0) || 1;
    const ragCount = be.routes ? (be.routes["local"] || be.routes["rag"] || be.routes["chat"] || 0) : 0;
    const searchCount = be.routes ? (be.routes["web"] || be.routes["deep"] || be.routes["web_search"] || be.routes["deep_research"] || 0) : 0;
    const actionCount = be.routes ? (be.routes["action"] || 0) : 0;
    
    const ragLoad = Math.round((ragCount / totalRoutes) * 100);
    const searchLoad = Math.round((searchCount / totalRoutes) * 100);
    const actionLoad = Math.round((actionCount / totalRoutes) * 100);
    
    $("rag-load").textContent = ragLoad + "%";
    $("rag-load-bar").style.width = ragLoad + "%";
    $("search-load").textContent = searchLoad + "%";
    $("search-load-bar").style.width = searchLoad + "%";
    $("action-load").textContent = actionLoad + "%";
    $("action-load-bar").style.width = actionLoad + "%";

    // Balken auf das haeufigste Werkzeug normiert -- eine absolute Skala waere bei
    // frisch gestartetem Backend nur eine Reihe leerer Striche.
    const tools = be.tools || {};
    const toolNames = Object.keys(tools);
    const toolBox = $("tool-usage");
    const toolEmpty = $("toolUsageEmpty");
    if (toolBox && toolEmpty) {
      toolEmpty.style.display = toolNames.length ? "none" : "block";
      const maxTool = toolNames.reduce(function (m, n) { return Math.max(m, tools[n]); }, 1);
      toolBox.innerHTML = toolNames.map(function (name) {
        const count = tools[name];
        const pct = Math.round((count / maxTool) * 100);
        // escapeHtml auch hier, damit ein spaeter ergaenztes Werkzeug nichts aufreisst.
        return "<div class='tool-usage-row'>" +
          "<span class='tool-usage-name'>" + escapeHtml(name) + "</span>" +
          "<span class='tool-usage-count'>" + count + "</span>" +
          "<div class='progress-container'><div class='progress-fill' style='width:" + pct + "%'></div></div>" +
          "</div>";
      }).join("");
    }

    // Evals und Audit sind Datei-/DB-lastig -> nur jeden 5. Tick.
    if (force || tickNum % 5 === 0) {
      // Evals Runs
      const ev = await api("/v1/dashboard/evals");
      const evRows = ev.evals || [];
      $("evalsEmpty").style.display = evRows.length ? "none" : "block";
      $("evals").innerHTML = evRows.map(function (r) {
        // metric/value duerfen null sein -- toLowerCase() darauf beendete sonst
        // den gesamten tick() im stillen catch.
        const metricText = String(r.metric || "");
        const metricLower = metricText.toLowerCase();
        const metricColor = metricLower.includes("fail") || metricLower.includes("error") ? "color:var(--color-magenta);" : "color:var(--text-primary);";
        const valueText = (r.value === null || r.value === undefined) ? "-" : r.value;
        // Felder stammen aus Run-Log-Dateien, teils modellgeneriert -- nicht als
        // Markup vertrauenswuerdig.
        return "<tr>" +
          "<td><span style='color:var(--text-secondary);'>#</span> " + tcell(r.ts) + "</td>" +
          "<td><span style='color:#fff; font-weight:600;'>" + escapeHtml(r.suite || "") + "</span></td>" +
          "<td><span style='color:var(--text-secondary);'>" + escapeHtml(r.config || "") + "</span></td>" +
          "<td><span style='" + metricColor + "'>" + escapeHtml(metricText) + "</span></td>" +
          "<td><span style='color:var(--color-green); font-weight:bold;'>" + escapeHtml(String(valueText)) + "</span></td>" +
          "</tr>";
      }).join("");

      // Audit logs success/failure color scheme
      const au = await api("/v1/dashboard/audit");
      const auRows = au.audit || [];
      $("auditEmpty").style.display = auRows.length ? "none" : "block";
      const aSub = $("audit-sub");
      if (aSub) aSub.textContent = "ACTION-ENGINE" + (auRows.length ? " \u00b7 LETZTE " + auRows.length : "");
      $("audit").innerHTML = auRows.map(function (r) {
        // Sekunden wie im Entwurf (0,42 s) statt Millisekunden -- die Zahlen
        // stehen neben Werkzeugdauern im Chat und sollen sich gleich lesen.
        const lat = (r.latency_ms === null || r.latency_ms === undefined)
          ? "\u2014" : (r.latency_ms / 1000).toFixed(2).replace(".", ",") + " s";
        const statLower = (r.status || "").toLowerCase();
        const isOk = statLower === "success" || statLower === "ok" || statLower === "approved";
        const statusClass = isOk ? "sys-online" : "err-blink";
        const statusDot = "<span class='status-indicator " + (isOk ? "online" : "offline") + "'></span>";
        const hasDetails = !!r.details;
        const cursorStyle = hasDetails ? "cursor: pointer;" : "";
        const chevron = hasDetails ? "<span class='expand-chevron'>" + icon("chevron", 10) + "</span>" : "";

        let rowHtml = "<tr onclick='if(window.toggleAuditRow) window.toggleAuditRow(this)' style='" + cursorStyle + "'>" +
          "<td><span style='color:var(--color-blue); opacity:0.5;'>»</span> " + tcell(r.ts) + "</td>" +
          // escapeHtml ist PFLICHT: der Text ist die erste Zeile des ausgefuehrten
          // PowerShell-Skripts und damit per Prompt-Injection steuerbar.
          "<td><span style='color:var(--color-blue); font-weight:bold;'>" + escapeHtml(getFriendlyActionName(r.tool, r.details)) + chevron + "</span></td>" +
          "<td><span style='color:var(--text-secondary);'>" + escapeHtml(r.tool_class || "") + "</span></td>" +
          "<td>" + statusDot + "<span class='" + statusClass + "'>" + escapeHtml(getStatusLabel(r.status)) + "</span></td>" +
          "<td><span style='color:var(--text-primary);'>" + lat + "</span></td>" +
          "</tr>";

        if (hasDetails) {
          rowHtml += "<tr class='audit-details-row' style='display: none; background: rgba(0,0,0,0.25);'>" +
            "<td colspan='5' style='padding: 10px 15px; border-bottom: 1px solid rgba(0, 136, 255, 0.15);'>" +
            "<pre style=\"margin: 0; font-family: var(--font-mono); font-size: 11px; color: #fff; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; background: rgba(0, 240, 255, 0.02); padding: 8px; border: 1px solid rgba(0, 240, 255, 0.1); border-radius: 4px;\">" + escapeHtml(r.details) + "</pre>" +
            "</td>" +
            "</tr>";
        }
        return rowHtml;
      }).join("");

      const mi = await api("/v1/dashboard/missions");
      const miRows = mi.missions || [];
      $("missionsEmpty").style.display = miRows.length ? "none" : "block";
      $("missions").innerHTML = miRows.map(function (r) {
        const stLower = (r.status || "").toLowerCase();
        const isDone = stLower === "done";
        const isBad = stLower === "failed" || stLower === "cancelled";
        const statusClass = isDone ? "sys-online" : (isBad ? "err-blink" : "");
        const dot = isDone ? "<span class='status-indicator online'></span>" : (isBad ? "<span class='status-indicator offline'></span>" : "");
        return "<tr onclick='if(window.toggleMissionRow) window.toggleMissionRow(this, " + r.id + ")' style='cursor: pointer;'>" +
          "<td><span style='color:var(--color-blue); font-weight:bold;'>#" + r.id + "</span></td>" +
          "<td>" + dot + "<span class='" + statusClass + "'>" + escapeHtml(r.status || "") + "</span></td>" +
          "<td>" + escapeHtml(r.provider || "") + "</td>" +
          "<td>" + (r.calls_used || 0) + "</td>" +
          "<td>" + (r.tokens_in || 0) + " / " + (r.tokens_out || 0) + "</td>" +
          "<td><span style='color:var(--text-secondary);'>" + escapeHtml(r.current_step || "") + "</span></td>" +
          "<td>" + escapeHtml((r.goal || "").slice(0, 90)) + "<span class='expand-chevron'>" + icon("chevron", 10) + "</span></td>" +
          // Loeschen nur bei beendeten Missionen -- laufende lehnt das Backend ab.
          "<td>" + ((isDone || isBad)
            ? "<span class='mission-del-btn' data-mission-id='" + r.id + "' title='Diese Mission loeschen'>&times;</span>"
            : "") + "</td>" +
          "</tr>" +
          "<tr class='mission-details-row' style='display: none; background: rgba(0,0,0,0.25);'>" +
          "<td colspan='8' style='padding: 10px 15px; border-bottom: 1px solid rgba(0, 136, 255, 0.15);'>" +
          "<pre class='mission-result-pre' style=\"margin: 0; font-family: var(--font-mono); font-size: 11px; color: #fff; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; background: rgba(0, 240, 255, 0.02); padding: 8px; border: 1px solid rgba(0, 240, 255, 0.1); border-radius: 4px;\">Klicken zum Laden...</pre>" +
          "</td></tr>";
      }).join("");

      // Eigener Listener je Knopf: die Zeile traegt schon einen onclick fuer die
      // Detailzeile, stopPropagation haelt die Klickziele auseinander.
      Array.prototype.forEach.call(
        document.querySelectorAll("#missions .mission-del-btn"),
        function (btn) {
          btn.addEventListener("click", async function (ev) {
            ev.stopPropagation();
            const mid = btn.dataset.missionId;
            if (!confirm("Mission #" + mid + " loeschen?")) return;
            try {
              await api("/v1/dashboard/missions/" + mid, { method: "DELETE" });
              await tick(true);
            } catch (err) {
              alert("Loeschen fehlgeschlagen: " + err.message);
            }
          });
        }
      );
    }
  } catch (e) {
    // Ein einzelnes Panel ist gestolpert; die Verbindung steht nachweislich (sonst
    // waere die Lebendprobe oben ausgestiegen). Kein Offline-Alarm.
  }
  finally { tickNum++; }
}

let timer = null;
let heartbeat = null;
const HEARTBEAT_MS = 15000;

// Tausender mit schmalem Leerzeichen -- 25 000 liest sich schneller als 25000.
// Deutsche Schreibweise: Komma als Dezimaltrenner, schmales Leerzeichen als
// Tausendertrenner. fmt() liefert die englische Form fuer die aelteren Panels.
function deZahl(v, stellen) {
  if (v === null || v === undefined) return "\u2013";
  const s = stellen ? Number(v).toFixed(stellen) : String(Math.round(v));
  const teile = s.split(".");
  return kvZahl(teile[0]) + (teile[1] ? "," + teile[1] : "");
}

function kvZahl(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u202f");
}

// Die fuenf Kacheln oben im Ueberblick. Sie fassen nur zusammen, was ohnehin
// abgefragt wird -- keine eigene Anfrage.
function kacheln(be, eng, tts, kvPct, verlauf) {
  const setz = function (id, wert) { const e = $(id); if (e) e.textContent = wert; };

  setz("k-durchsatz", eng.gen_throughput_tok_s ? deZahl(eng.gen_throughput_tok_s, 1) : "0");
  if (verlauf && verlauf.length > 1) {
    updateSparkline("k-spark", verlauf, Math.max(...verlauf, 50));
  }

  setz("k-ttft", be.avg_ttft_ms ? deZahl(be.avg_ttft_ms / 1000, 2) : "\u2013");
  setz("k-lauf", be.avg_total_ms ? "Ø LAUF " + deZahl(be.avg_total_ms / 1000, 1) + " S" : "");

  setz("k-kv", kvPct);
  const kvBar = $("k-kv-bar");
  if (kvBar) kvBar.style.width = Math.max(0, Math.min(100, kvPct)) + "%";
  const komp = be.compactions_total || 0;
  let kvSub = eng.kv_pool_tokens ? kvZahl(eng.kv_pool_tokens) + " TOKEN" : "";
  if (komp) {
    const l = be.last_compaction;
    const zeit = l && l.ts
      ? " \u00b7 LETZTE " + String(new Date(l.ts * 1000).getHours()).padStart(2, "0")
        + ":" + String(new Date(l.ts * 1000).getMinutes()).padStart(2, "0")
      : "";
    kvSub += (kvSub ? " \u00b7 " : "") + "KOMPAKTIERT " + komp + "\u00d7" + zeit;
  }
  setz("k-kv-sub", kvSub);

  setz("k-req", be.requests_total !== undefined ? deZahl(be.requests_total) : "\u2013");
  setz("k-req-sub", "IN ARBEIT " + (eng.running_reqs || 0)
                 + " \u00b7 WARTESCHLANGE " + (eng.queue_reqs || 0));

  const treffer = eng.cache_hit_rate !== undefined ? Math.round(eng.cache_hit_rate * 100) : null;
  setz("k-cache", treffer === null ? "\u2013" : treffer);
  const cb = $("k-cache-bar");
  if (cb) cb.style.width = (treffer || 0) + "%";
  //--- Der tts-service misst RTF und Latenz selbst; solange nichts gesprochen
  //--- wurde, sind beide 0 und die Zeile bleibt leer statt Nullen zu zeigen.
  const rtf = tts && tts.avg_rtf, lat = tts && tts.avg_latency_s;
  setz("k-tts-sub", rtf ? "TTS RTF " + deZahl(rtf, 2)
                        + (lat ? " \u00b7 LATENZ " + Math.round(lat * 1000) + " MS" : "") : "");
}

// Die vier Zeilen des SYSTEM-Panels. Alle Werte kommen aus /summary; wo eine
// Angabe fehlt, bleibt der Gedankenstrich stehen statt einer erfundenen Zahl.
function systemPanel(s) {
  const cfg = s.config || {}, tts = s.tts || {};
  const setz = function (id, text, klasse) {
    const e = $(id);
    if (!e) return;
    e.textContent = text;
    e.className = "sys-wert" + (klasse ? " " + klasse : "");
  };

  const mn = $("model-name");
  if (mn) mn.textContent = s.model || "\u2013";

  setz("sys-embed", cfg.embedding_model
    ? cfg.embedding_model.replace("BAAI/", "") + " (lokal) \u00b7 k = " + cfg.rag_k
    : "\u2013", "sys-cyan");

  const online = !!s.searxng_online;
  setz("sys-suche", "SearxNG " + (online ? "online" : "offline")
    + (cfg.web_timeout_ms ? " \u00b7 Limit " + kvZahl(cfg.web_timeout_ms) + " ms" : ""),
    online ? "sys-gruen" : "sys-magenta");

  //--- Die Stimme meldet der tts-service mit seinen Kennzahlen; RTF erst, wenn
  //--- wirklich gesprochen wurde -- sonst stuende dort eine Null ohne Messung.
  const stimme = tts.voice ? ("Qwen3 \u00b7 " + tts.voice + (tts.lang ? " \u00b7 " + tts.lang : "")) : "Qwen3-TTS";
  setz("sys-tts", stimme + (tts.avg_rtf ? " \u00b7 RTF " + deZahl(tts.avg_rtf, 2) : ""));
}

// Kopfzeile: Prozent, Balken und "7x - 14:18" (wie oft verdichtet, wann zuletzt).
// compactions_total zaehlt ab dem Prozessstart, nicht ab Mitternacht -- der
// Zaehlerstand steht deshalb ohne Zeitbezug da.
function navKv(pct, be, pool) {
  const box = $("nav-kv");
  if (!box) return;
  const p = $("nav-kv-pct"), fill = $("nav-kv-fill"), comp = $("nav-kv-comp");
  // Prozent wie im Entwurf. Der absolute Bezug -- der Pool wird bei jedem Start
  // neu ausgehandelt -- steht im Tooltip weiter unten.
  if (p) p.textContent = pct + " %";
  if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
  box.classList.toggle("is-hoch", pct >= 80);
  if (!comp) return;
  const total = (be && be.compactions_total) || 0;
  if (!total) { comp.textContent = ""; return; }
  const last = be && be.last_compaction;
  let zeit = "";
  if (last && last.ts) {
    const d = new Date(last.ts * 1000);
    zeit = " \u00b7 " + String(d.getHours()).padStart(2, "0") + ":"
         + String(d.getMinutes()).padStart(2, "0");
  }
  comp.textContent = total + "\u00d7" + zeit;
  box.title = "KV-Cache: " + pct + "% von " + (pool ? kvZahl(pool) + " Token" : "unbekannt")
            + ". " + total + "x verdichtet"
            + (zeit ? ", zuletzt um" + zeit.replace(" \u00b7 ", " ") : "") + ".";
}

// Lebendprobe ohne die Panels neu zu zeichnen. Das Status-Pill steht in der
// Navigation und ist in JEDER Ansicht sichtbar. Bewusst /config statt /summary:
// die Kennzahlen sind Administratoren vorbehalten, ein Benutzer-Konto braucht aber
// dieselbe Aussage darueber, ob das Backend lebt.
async function probeConnection() {
  try {
    await api("/v1/dashboard/config");
    noteConnection(true);
    //--- Die Kopfzeile lebt in JEDER Ansicht, der Monitor-Poll laeuft nur im
    //    Monitor. Ein Administrator holt sich die Zahl deshalb hier mit -- ein
    //    Benutzer-Konto darf /summary nicht und ueberspringt es.
    if (window.argusAuth && window.argusAuth.isAdmin()
        && !timer) {
      try {
        const sum = await api("/v1/dashboard/summary");
        const eng = (sum && sum.engine) || {};
        navKv(eng.kv_cache_usage !== undefined ? Math.round(eng.kv_cache_usage * 100) : 0,
              (sum && sum.backend) || {}, eng.kv_pool_tokens || 0);
      } catch (e) { /* Kopfzeile ist Beiwerk -- die Lebendprobe zaehlt */ }
    }
  } catch (e) {
    noteConnection(false);
  }
}

function start() {
  if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
  const leeren = $("coll-clear");
  if (leeren && !leeren.dataset.gebunden) {
    leeren.dataset.gebunden = "1";
    leeren.addEventListener("click", clearCollection);
  }
  const upl = $("coll-upload");
  if (upl && !upl.dataset.gebunden) {
    upl.dataset.gebunden = "1";
    upl.addEventListener("click", function () {
      if (window.argusFiles && window.argusFiles.open) window.argusFiles.open();
    });
  }
  const ing = $("coll-ingest");
  if (ing && !ing.dataset.gebunden) {
    ing.dataset.gebunden = "1";
    ing.addEventListener("click", startCollectionIngest);
  }
  refreshDocsList();
  // force=true: der erste Tick nach dem Betreten muss AUCH die schweren Panels holen,
  // sonst entscheidet der Stand von tickNum darueber, ob die Tabellen gefuellt sind.
  tick(true);
  if (timer) clearInterval(timer);
  timer = setInterval(tick, 3000);
}

// Vom Router beim Verlassen der Monitor-Ansicht gerufen: sonst liefe das 3s-Polling
// weiter, wenn niemand die Zahlen anschaut. Der Herzschlag bleibt -- eine Anfrage
// alle 15 Sekunden.
function stopPolling() {
  if (timer) { clearInterval(timer); timer = null; }
  if (!heartbeat) heartbeat = setInterval(probeConnection, HEARTBEAT_MS);
}

// Erst nach der Anmeldung: vorher lieferte jeder Aufruf 401 und riefe die
// Anmeldemaske erneut auf den Plan.
function startHeartbeat() {
  if (heartbeat) clearInterval(heartbeat);
  heartbeat = setInterval(probeConnection, HEARTBEAT_MS);
  probeConnection();
}

// Beim Abmelden: alles anhalten, auch den Herzschlag. Liefe er weiter, klopfte die
// Seite im Minutentakt gegen eine Tuer, die sie gerade selbst geschlossen hat.
function halt() {
  if (timer) { clearInterval(timer); timer = null; }
  if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
}

window.argusOps = { start: start, stop: stopPolling, tick: tick,
                    heartbeat: startHeartbeat, halt: halt };

// Lesbare Groesse. null = die Datei liegt nicht mehr im Arbeitsverzeichnis;
// der Index kennt sie trotzdem noch.
function collGroesse(bytes) {
  if (bytes === null || bytes === undefined) return "\u2013";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function collZeit(iso) {
  if (!iso) return "\u2013";
  const d = argusDate(iso);
  if (isNaN(d)) return "\u2013";
  const heute = new Date();
  const gleicherTag = d.toDateString() === heute.toDateString();
  const uhr = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  return gleicherTag ? uhr
    : String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + ". " + uhr;
}

async function refreshDocsList() {
  const container = $("docs-list-container");
  if (!container) return;
  try {
    const res = await api("/v1/dashboard/documents");
    const docs = res.documents || [];
    const zahl = $("coll-zahl");
    if (zahl) {
      const chunks = docs.reduce(function (a, d) { return a + (d.chunk_count || 0); }, 0);
      zahl.textContent = docs.length
        ? docs.length + " DOKUMENTE · " + kvZahl(chunks) + " CHUNKS" : "LEER";
    }
    if (!docs.length) {
      container.innerHTML = '<div class="muted">Keine Dokumente indexiert.</div>';
      return;
    }
    // Keine Inline-Handler mit interpoliertem Dateinamen: der Browser dekodiert die
    // Entities vor dem JS-Parsen, ein Apostroph braeche den Handler.
    container.innerHTML = docs.map(function (d) {
      return '<div class="coll-row" id="doc-row-' + d.id + '">'
        + '<span class="coll-sym">' + icon("datei", 14) + '</span>'
        + '<span class="coll-name" title="' + escapeHtml(d.filename) + '">'
        + escapeHtml(d.filename) + '</span>'
        + '<span class="coll-num">' + (d.chunk_count || 0) + ' Chunks</span>'
        + '<span class="coll-num">' + collGroesse(d.size_bytes) + '</span>'
        + '<span class="coll-num">' + collZeit(d.ingested_at) + '</span>'
        + '<button class="coll-del" type="button" title="Aus dem Index loeschen" '
        + 'data-doc-id="' + d.id + '" data-doc-name="' + escapeHtml(d.filename) + '">'
        + icon("trash", 14) + '</button>'
        + '</div>';
    }).join("");
    container.querySelectorAll(".coll-del").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        deleteDoc(Number(btn.dataset.docId), btn.dataset.docName, ev);
      });
    });
  } catch (e) {
    container.innerHTML = '<div class="muted" style="color:var(--color-magenta);">Fehler beim Laden.</div>';
  }
}

// Indexiert alles, was im Uploads-Ordner liegt. Der Endpunkt ruft je Datei
// denselben Weg wie der Einzel-Ingest -- gleiche Zerlegung, gleiche Collection.
async function startCollectionIngest() {
  const b = $("coll-ingest");
  if (b) { b.disabled = true; b.textContent = "INDEXIERE \u2026"; }
  try {
    const r = await apiPost("/v1/dashboard/uploads/ingest_all");
    await refreshDocsList();
    await tick(true);
    const fehler = (r.ergebnisse || []).filter(function (e) { return e.status === "fehler"; });
    alert(r.verarbeitet
      ? r.verarbeitet + " Eintraege verarbeitet, " + (r.indexiert || 0) + " Dateien indexiert."
        + (fehler.length ? "\n\nFehler bei: " + fehler.map(function (e) { return e.name; }).join(", ") : "")
      : "Nichts zu indexieren \u2013 im Uploads-Ordner liegt keine lesbare Datei.");
  } catch (e) {
    alert("Ingest fehlgeschlagen: " + e.message);
  } finally {
    if (b) { b.disabled = false; b.textContent = "INGEST STARTEN"; }
  }
}

// Leert den INDEX, nicht das Arbeitsverzeichnis -- das steht auch in der Rueckfrage,
// weil "Collection leeren" sonst nach mehr klingt als es ist.
async function clearCollection() {
  if (!confirm("Die ganze Collection aus dem Index loeschen?\n\n"
             + "Die Dateien im Arbeitsverzeichnis bleiben liegen.")) return;
  const knopf = $("coll-clear");
  if (knopf) { knopf.disabled = true; knopf.textContent = "LEERE \u2026"; }
  try {
    const r = await api("/v1/dashboard/documents", { method: "DELETE" });
    await refreshDocsList();
    await tick(true);
    if (r && r.errors) alert(r.deleted + " geloescht, " + r.errors + " mit Fehlern in Qdrant.");
  } catch (e) {
    alert("Collection nicht leerbar: " + e.message);
  } finally {
    if (knopf) { knopf.disabled = false; knopf.textContent = "COLLECTION LEEREN"; }
  }
}

async function deleteDoc(id, filename, event) {
  event.stopPropagation();
  if (!confirm(`Möchtest du das Dokument "${filename}" wirklich aus der Datenbank und Qdrant löschen?`)) {
    return;
  }
  const row = $(`doc-row-${id}`);
  if (row) {
    row.style.opacity = "0.5";
  }
  try {
    await api(`/v1/dashboard/documents/${id}`, { method: "DELETE" });
    await refreshDocsList();
    await tick(true);
  } catch (e) {
    alert("Fehler beim Löschen des Dokuments: " + e.message);
    if (row) {
      row.style.opacity = "1";
    }
  }
}

$("evals-panel-header").addEventListener("click", () => togglePanel("evals-panel"));
$("audit-panel-header").addEventListener("click", () => togglePanel("audit-panel"));
$("missions-panel-header").addEventListener("click", () => togglePanel("missions-panel"));

// Quelle erst beim Betreten der Monitor-Ansicht setzen, sonst zieht jeder Aufruf
// der Live-Ansicht die komplette Phoenix-Oberflaeche mit.
// Die Adresse kommt vom Backend (DASHBOARD_TRACES_URL): auf einem Server ist
// Phoenix nicht unter 127.0.0.1 des Browsers erreichbar. Leer = nicht eingebunden.
let tracesUrl = null;

async function ensureTracesLoaded() {
  const f = $("traces-frame");
  if (!f) return;
  if (tracesUrl === null) {
    try {
      const cfg = await api("/v1/dashboard/config");
      tracesUrl = (cfg && cfg.config && cfg.config.traces_url) || "";
    } catch (e) {
      return;  // naechster Besuch des Reiters versucht es erneut
    }
    const adresse = $("traces-adresse");
    const extern = $("traces-extern");
    const hint = $("traces-hint");
    if (tracesUrl) {
      if (adresse) adresse.textContent = "PHOENIX · " + tracesUrl.replace(/^https?:\/\//, "").toUpperCase();
      if (extern) extern.href = tracesUrl;
      f.dataset.src = tracesUrl + "/projects";
    } else {
      if (adresse) adresse.textContent = "NICHT EINGEBUNDEN";
      if (extern) extern.style.display = "none";
      f.style.display = "none";
      if (hint) hint.textContent = "Die Ablaufverfolgung ist in dieser Installation nicht eingebunden (DASHBOARD_TRACES_URL ist leer).";
    }
  }
  if (!f.src && f.dataset.src) f.src = f.dataset.src;
}
window.argusTraces = { ensureLoaded: ensureTracesLoaded };

// ===== Unterreiter im Monitor =====
// Dasselbe .active-Muster wie beim Hauptrouter. Der Hash bleibt einsegmentig --
// der aktive Reiter ist Anzeigezustand, keine Adresse zum Verlinken.
let monitorTab = "overview";

function setMonitorTab(name) {
  monitorTab = name;
  document.querySelectorAll(".subtab").forEach(function (b) {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".subview").forEach(function (v) {
    v.classList.toggle("active", v.id === "tab-" + name);
  });
  // Erst beim Öffnen laden: sonst zieht jeder Blick in den Monitor die komplette
  // Phoenix-Oberfläche mit.
  if (name === "traces") ensureTracesLoaded();
  if (name === "accounts" && window.argusUsers) window.argusUsers.refresh();
  if (name === "overview") refreshDocsList();
}

document.querySelectorAll(".subtab").forEach(function (b) {
  b.addEventListener("click", function () { setMonitorTab(b.dataset.tab); });
});

window.argusMonitorTabs = { current: function () { return monitorTab; },
                            set: setMonitorTab };

const btnTracesReload = $("btnTracesReload");
if (btnTracesReload) {
  btnTracesReload.addEventListener("click", function () {
    const f = $("traces-frame");
    if (!f || !f.dataset.src) return;
    // Neu zuweisen statt reload(): auf ein Dokument fremder Herkunft (Port 6006)
    // darf die Seite nicht zugreifen.
    f.src = (f.dataset.src || "") + "?t=" + Date.now();
  });
}

// Ein Browser darf von einer http-Seite weder Explorer noch file:// oeffnen, und
// das Backend sitzt im Container ohne Desktop -- der Pfad geht in die Zwischenablage.
async function copyToClipboard(text) {
  // Zwei Wege: navigator.clipboard verlangt einen sicheren Kontext (faellt bei
  // http://<rechnername>:7860 weg), execCommand funktioniert dort weiterhin.
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) { /* zweiter Weg unten */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (err) {
    return false;
  }
}

function bindCopyPath(id) {
  const btn = $(id);
  if (!btn) return;
  btn.addEventListener("click", async function (e) {
    e.stopPropagation();
    const path = btn.dataset.path || "";
    const label = btn.dataset.label || btn.innerHTML;
    const titel = btn.dataset.titel || btn.title;
    btn.dataset.label = label;
    btn.dataset.titel = titel;
    const ok = await copyToClipboard(path);
    btn.innerHTML = icon(ok ? "haken" : "strich", 13) + escapeHtml(path);
    btn.title = ok ? "Kopiert -- im Explorer mit Strg+V in die Adresszeile"
                   : "Kopieren nicht moeglich -- Pfad von Hand markieren";
    setTimeout(function () { btn.innerHTML = label; btn.title = titel; }, 4000);
  });
}
bindCopyPath("btnCopyEvalPath");
bindCopyPath("btnCopyAuditPath");

$("btnClearMissions").addEventListener("click", async (e) => {
  e.stopPropagation();
  if (!confirm("Alle BEENDETEN Missionen loeschen? Laufende bleiben erhalten.")) return;
  try {
    const res = await api("/v1/dashboard/missions", { method: "DELETE" });
    alert(res.message || "Missionen geloescht.");
    await tick(true);
  } catch (err) {
    alert("Fehler: " + err.message);
  }
});

$("btnOpenFolder").addEventListener("click", async (e) => {
  e.stopPropagation();
  const box = $("evalLogsBox");
  const willShow = box.style.display === "none";
  box.style.display = willShow ? "block" : "none";
  $("btnOpenFolder").textContent = willShow ? "📂 Run-Logs ausblenden" : "📂 Run-Logs anzeigen";
  if (!willShow) return;
  $("evalLogContent").style.display = "none";
  $("evalLogsList").innerHTML = "<span class='muted'>Lade …</span>";
  try {
    const res = await api("/v1/dashboard/eval_logs");
    if (res.dir) $("evalLogsDir").textContent = res.dir;
    const files = res.files || [];
    if (!files.length) {
      // Die Tabelle oben speist sich aus GENAU diesem Ordner (evals liest die
      // Dateien, nicht die DB) -- ein Lauf ohne Datei taucht nirgends auf.
      $("evalLogsList").innerHTML = "<span class='muted'>Keine Run-Log-Dateien in diesem Ordner. GAIA und HumanEval legen ihre JSON-Dateien hier ab; die Tabelle oben zeigt genau diese Dateien.</span>";
      return;
    }
    $("evalLogsList").innerHTML = files.map(function (f) {
      const kb = (f.size / 1024).toFixed(1);
      // data-name bleibt encodeURIComponent (URL-Kontext), die Anzeige braucht HTML.
      return "<div class='eval-log-row' data-name='" + encodeURIComponent(f.name) + "' " +
        "style='cursor:pointer; padding:6px 8px; border-bottom:1px solid rgba(255,255,255,0.06); display:flex; justify-content:space-between; gap:10px;'>" +
        "<span style='color:var(--color-cyan);'>📄 " + escapeHtml(f.name) + "</span>" +
        "<span class='muted' style='font-size:0.74rem;'>" + tcell(f.mtime) + " · " + kb + " KB</span>" +
        "</div>";
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll("#evalLogsList .eval-log-row"), function (row) {
      row.addEventListener("click", async function () {
        const nm = decodeURIComponent(row.getAttribute("data-name"));
        const pre = $("evalLogContent");
        pre.style.display = "block";
        pre.textContent = "Lade " + nm + " …";
        try {
          const data = await api("/v1/dashboard/eval_logs/" + encodeURIComponent(nm));
          pre.textContent = JSON.stringify(data, null, 2);
        } catch (err) {
          pre.textContent = "Fehler beim Laden: " + err.message;
        }
      });
    });
  } catch (err) {
    $("evalLogsList").innerHTML = "<span class='muted'>Fehler: " + err.message + "</span>";
  }
});

$("btnClearEvals").addEventListener("click", async (e) => {
  e.stopPropagation();
  if (!confirm("Möchtest du den gesamten Eval-Verlauf wirklich löschen?")) return;
  try {
    await api("/v1/dashboard/evals", { method: "DELETE" });
    await tick(true);
  } catch (err) {
    alert("Fehler beim Bereinigen: " + err.message);
  }
});

$("btnClearAudit").addEventListener("click", async (e) => {
  e.stopPropagation();
  if (!confirm("Audit-Log aus der DB löschen? (JSONL-Dateien bleiben erhalten)")) return;
  try {
    const res = await api("/v1/dashboard/audit", { method: "DELETE" });
    alert(res.message || "Audit-Log gelöscht.");
    await tick(true);
  } catch (err) {
    alert("Fehler: " + err.message);
  }
});

// Der Schwebe-Hinweis gehoerte zur Sparkline im entfallenen Panel. Die Kachel
// zeigt den Verlauf ohne Beschriftung.
