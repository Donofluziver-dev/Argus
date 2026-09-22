#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setup_Dashboard.py -- generiert die Argus-Chat-Oberflaeche unter rag_backend/static/.

Setup1 ruft main() am Ende mit auf; das Skript laeuft auch solo:
python Setup_Dashboard.py

Geschriebene Dateien (kein Build-Schritt, StaticFiles liefert den Ordner aus):

    static/index.html      Shell: Anmeldung, Navigation, View-Container
    static/argus.css       Stylesheet
    static/js/common.js    $-Helper, escapeHtml, api(), Sparkline
    static/js/auth.js      Anmeldung, Rollen, Kontoverwaltung
    static/js/bg.js        WebGL-Hintergrund (Nebel + Sterne)
    static/js/orb.js       WebGL-Partikel-Orb + KV-Cache-Kugel
    static/js/voice.js     SSE-Chat-Stream, Mikro/STT, TTS (Live-Ansicht)
    static/js/ops.js       Metriken, Evals, Audit, Missionen (Monitor-Ansicht)
    static/js/files.js     Upload, Entpacken, Indexieren (Dateien-Ansicht)
    static/js/chat.js      Chat-Ansicht
    static/js/router.js    Hash-Router; startet/stoppt Orb und Polling

Ladereihenfolge in index.html ist bindend: common.js definiert die Helfer, die
alle uebrigen Dateien benutzen.

Vier Ansichten: Live (Orb, Sprache), Chat, Dateien und Monitor. Zugang ueber
Anmeldung mit zwei Rollen -- 'admin' sieht alles, 'chat' nur Live und Chat. Die
Rollen-Pruefung im Browser blendet nur aus; verbindlich sind die Gates an den
Routen (dashboard_user / dashboard_admin in main.py).

ACHTUNG Kopplung: TOOL_COLORS in orb.js haengt ueber die Status-Emojis an
_TOOL_STATUS in Setup2 (agent.py) -- aendern sich dort Emojis, hier nachziehen.
"""
import re
from pathlib import Path

try:
    from setup_common import _log, writefile
except ImportError:
    raise SystemExit("setup_common.py fehlt -- bitte zuerst 'python Setup1.py' ausfuehren.")

BASE_DIR = Path(__file__).parent.resolve()

INDEX_HTML = r'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Argus Chat</title>
<link rel="stylesheet" href="argus.css">
<link rel="stylesheet" href="argus-glass.css">
</head>
<body>
<canvas id="bg-canvas" aria-hidden="true"></canvas>
<div class="bg-glow bg-glow-1"></div>
<div class="bg-glow bg-glow-2"></div>
<div class="bg-glow bg-glow-3"></div>

<!-- Anmeldung. Liegt ueber allem und wird erst entfernt, wenn eine Sitzung steht. -->
<div class="auth-gate" id="auth-gate">
  <canvas id="auth-orb-canvas"></canvas>
  <div class="auth-card">
    <div class="auth-logo" id="auth-logo"></div>
    <div class="auth-brand">ARGUS</div>
    <div class="auth-sub" id="auth-sub">LOKALER AGENT &middot; ZUGANG ERFORDERLICH</div>
    <form class="auth-form" id="auth-form" autocomplete="on">
      <label class="auth-label" for="auth-user">BENUTZER</label>
      <span class="auth-feld"><i class="auth-feld-icon" data-icon="person"></i>
        <input class="auth-input" id="auth-user" name="username" autocomplete="username"
               autocapitalize="none" spellcheck="false" required></span>
      <label class="auth-label" for="auth-pass">PASSWORT</label>
      <span class="auth-feld"><i class="auth-feld-icon" data-icon="schloss"></i>
        <input class="auth-input" id="auth-pass" name="password" type="password"
               autocomplete="current-password" required></span>
      <div class="auth-row" id="auth-confirm-row" style="display:none;">
        <label class="auth-label" for="auth-pass2">PASSWORT WIEDERHOLEN</label>
        <span class="auth-feld"><i class="auth-feld-icon" data-icon="schloss"></i>
          <input class="auth-input" id="auth-pass2" type="password" autocomplete="new-password"></span>
      </div>
      <label class="auth-remember" id="auth-remember-row">
        <input type="checkbox" id="auth-remember">
        <span>Angemeldet bleiben <span class="auth-remember-hint">(7 Tage)</span></span>
      </label>
      <button class="auth-btn" id="auth-submit" type="submit">
        <span id="auth-submit-text">Anmelden</span><i data-icon="pfeil"></i>
      </button>
      <div class="auth-msg" id="auth-msg"></div>
      <button type="button" class="auth-switch" id="auth-switch" hidden></button>
    </form>
    <!-- Der Punkt meldet, ob das Backend geantwortet hat. Rechts steht, was
         hinter der Tuer laeuft -- eine Aufzaehlung, keine Statusaussage. -->
    <div class="auth-fuss">
      <span class="auth-dienst" id="auth-dienst"><i></i>PR&Uuml;FE DIENST</span>
      <span class="auth-stack">SGLANG &middot; QDRANT &middot; QWEN-TTS</span>
    </div>
  </div>
</div>

<div class="dashboard-container" id="app-shell" hidden>

  <nav class="argus-nav">
    <span class="logo-area">
      <span class="logo-icon"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="4.6" opacity=".45"/><circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/><path d="M12 1.6v2.6M12 19.8v2.6M1.6 12h2.6M19.8 12h2.6"/></svg></span>
      <span class="logo-text"><h2>ARGUS</h2></span>
    </span>
    <!-- Eigener Kasten: die Kopfzeile setzt 20 px zwischen ihre Gruppen, die drei
         Reiter stehen untereinander aber mit 4 px eng zusammen. -->
    <div class="nav-links">
      <a class="nav-link" href="#/live"    data-view="live">Live</a>
      <a class="nav-link" href="#/chat"    data-view="chat">Chat</a>
      <a class="nav-link" href="#/monitor" data-view="monitor" data-role="admin">Monitor</a>
    </div>
    <!-- Dehnt sich, statt margin-left:auto an KV und Status zu haengen: die KV-Anzeige
         faellt bei einem 'chat'-Konto weg, und zwei auto-Raender teilten den Rest. -->
    <span class="nav-fill"></span>
    <!-- KV-Auslastung: Prozent, Balken und wie oft/wann verdichtet wurde. Nur fuer
         Administratoren -- die Zahl kommt aus /summary, das ein 'chat'-Konto nicht darf. -->
    <span class="nav-kv" id="nav-kv" data-role="admin" title="KV-Cache-Auslastung">
      <span class="nav-kv-label">KV</span>
      <span class="nav-kv-pct" id="nav-kv-pct">&mdash;</span>
      <span class="nav-kv-bar"><i id="nav-kv-fill"></i></span>
      <span class="nav-kv-comp" id="nav-kv-comp"></span>
    </span>
    <span class="nav-status" id="nav-status">
      <span class="pulse-dot"></span>
      <span class="status-text" id="nav-status-text">ONLINE</span>
    </span>

    <div class="nav-account">
      <button class="nav-account-btn" id="account-btn" aria-haspopup="true" aria-expanded="false">
        <span class="account-dot" id="account-dot"></span>
        <span class="account-ident">
          <span class="account-ident-name" id="account-name">&mdash;</span>
          <span class="account-ident-rolle" id="account-role">&mdash;</span>
        </span>
      </button>
      <div class="account-menu" id="account-menu" hidden>
        <button class="account-menu-item" id="account-password">Passwort &auml;ndern</button>
      </div>
    </div>
    <button class="nav-logout" id="account-logout" type="button"
            title="Abmelden" aria-label="Abmelden"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4.6H6.4a1.8 1.8 0 0 0-1.8 1.8v11.2a1.8 1.8 0 0 0 1.8 1.8H14"/><path d="M17.4 8.6 20.8 12l-3.4 3.4"/><path d="M20.8 12H10.2"/></svg></button>
  </nav>

  <!-- ===================== VIEW: LIVE (Sprachmodus) =====================
       Vollbild, dunkel, nur Orb und Sprache. Der Verlauf gehoert in die
       Chat-Ansicht -- hier soll nichts vom Zuhoeren ablenken. -->
  <section class="view" id="view-live">
    <div class="live-stage">
      <canvas id="galaxy-canvas" width="360" height="360"></canvas>

      <div class="live-state" id="vc-statelabel">
        <span class="live-state-wort"><span class="live-state-punkt"></span><span id="vc-statewort">Bereit</span></span>
        <span class="live-state-sub" id="vc-substate">Warte auf Eingabe</span>
      </div>

      <!-- Untertitel: nur der laufende Wortwechsel. Ohne ihn sieht man nicht,
           ob die Spracherkennung richtig verstanden hat. -->
      <div class="live-caption" id="vc-caption">
        <div class="live-caption-user" id="vc-caption-user"></div>
        <div class="live-caption-ai" id="vc-caption-ai"></div>
      </div>

      <div class="live-controls">
        <button class="live-btn" id="vc-tts" aria-label="Sprachausgabe an/aus"
                title="Sprachausgabe an/aus"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5z"/><path d="M16 9a4 4 0 0 1 0 6"/><path d="M19 6.5a8 8 0 0 1 0 11"/></svg></button>
        <button class="live-mic" id="vc-mic" aria-label="Sprechen"
                title="Sprechen (Leertaste)">
          <span class="live-mic-ring"></span>
          <span class="live-mic-glyph"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 19v3"/></svg></span>
        </button>
        <button class="live-btn" id="vc-keyboard" aria-label="Stattdessen tippen"
                title="Stattdessen tippen"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/></svg></button>
      </div>

      <div class="live-typebar" id="vc-typebar" hidden>
        <input id="vc-text" placeholder="Nachricht eingeben&hellip;" autocomplete="off">
        <button class="live-btn" id="vc-send" aria-label="Senden" title="Senden"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h15"/><path d="M13 6l6 6-6 6"/></svg></button>
      </div>

      <div class="live-hint" id="vc-hint">Leertaste zum Sprechen &middot; Esc bricht ab</div>
    </div>
  </section>

  <!-- ===================== VIEW: CHAT ===================== -->
  <section class="view" id="view-chat">
    <!-- Ohne 'panel': Verlauf und Unterhaltung sind im Entwurf zwei eigene Karten,
         die Huelle ist nur das Raster dazwischen. -->
    <div class="chat-shell">
      <aside class="chat-sidebar">
        <button id="chat-new" class="chat-new-btn">Neuer Chat</button>
        <div class="chat-suche">
          <span class="chat-suche-icon" id="chat-suche-icon"></span>
          <input id="chat-suche" type="search" placeholder="Verlauf durchsuchen"
                 autocomplete="off" aria-label="Verlauf durchsuchen">
        </div>
        <div id="chat-list" class="chat-list"><div class="muted">Lade&hellip;</div></div>
      </aside>
      <main class="chat-main">
        <div class="chat-head">
          <span id="chat-title">Neuer Chat</span>
          <span class="chat-head-meta" id="chat-meta"></span>
          <span class="chat-head-fill"></span>
          <span class="chat-head-kv" id="chat-head-kv"></span>
          <button class="chat-export" id="chat-export" type="button"
                  title="Verlauf als Markdown herunterladen">EXPORT</button>
        </div>
        <div id="chat-messages" class="chat-messages"></div>
        <div class="chat-attachments" id="chat-attachments" style="display: none;"></div>
        <div class="chat-composer">
          <!-- Ordner- und Klammer-Knopf sind bewusst weg: Dateien werden in den
               Chat GEZOGEN (siehe chat.js, Drop auf #view-chat), der Ordner war
               nur ein Sprung in den Monitor. -->
          <!-- Bild-Knopf nur, wenn die Engine multimodal laeuft (siehe chat.js).
               Eigenes Zeichen, damit er nicht mit der Klammer verwechselt wird. -->
          <button id="chat-attach" class="chat-attach-btn" title="Bild anhängen" aria-label="Bild anhängen" style="display: none;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.4" y="5.4" width="17.2" height="13.2" rx="2.2"/><circle cx="8.6" cy="10" r="1.6"/><path d="M4.2 16.4 9 12l3.2 3 2.8-2.4 4.6 3.8"/></svg></button>
          <input type="file" id="chat-file" accept="image/*" multiple hidden>
          <textarea id="chat-input" rows="1" placeholder="Nachricht an ARGUS&hellip;"></textarea>
          <button id="chat-send" class="chat-send-btn" title="Senden (Enter)" aria-label="Senden"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.4 12h13.2M12.4 6.2l5.8 5.8-5.8 5.8"/></svg></button>
        </div>
        <div class="chat-fuss">
          <span>ENTER SENDET &middot; SHIFT+ENTER NEUE ZEILE</span>
          <span class="chat-head-fill"></span>
          <span id="chat-fuss-kontext"></span>
        </div>
      </main>
    </div>
  </section>


  <!-- ===================== DIALOG: DATEIEN =====================
       Kein eigener Menuepunkt mehr: der Upload gehoert dorthin, wo man ueber die
       Datei spricht. Geoeffnet aus der Chat-Ansicht, nur fuer Administratoren. -->
  <div class="modal" id="files-modal" hidden>
    <div class="modal-backdrop" id="files-modal-backdrop"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="files-modal-title">
      <div class="modal-head">
        <span id="files-modal-title">ANHANG</span>
        <span class="modal-path">IN DEN CHAT LADEN ODER INGESTIEREN</span>
        <span class="chat-head-fill"></span>
        <button class="modal-close" id="files-modal-close" aria-label="Schließen">&times;</button>
      </div>

      <div class="modal-body">
        <div class="dropzone" id="dropzone" tabindex="0" role="button"
             aria-label="Dateien auswählen oder hierher ziehen">
          <div class="dropzone-icon" id="dropzone-icon"></div>
          <div class="dropzone-title">Dateien hierher ziehen</div>
          <div class="dropzone-hint" id="dropzone-hint">PDF &middot; DOCX &middot; MD &middot; TXT &middot; JSON</div>
          <button class="dropzone-btn" id="dropzone-btn" type="button">Durchsuchen</button>
          <input type="file" id="files-input" multiple hidden>
        </div>

        <div class="upload-queue" id="upload-queue"></div>

        <!-- Was bereits im Arbeitsverzeichnis liegt und auf ein Ziel wartet. -->
        <div class="upload-liste" id="upload-liste"></div>

        <!-- Zwei Ziele statt einer Dateiliste: der Dialog ist der Weg HINEIN.
             Verwalten und Loeschen liegt im Monitor unter Collection. -->
        <div class="upload-ziele" id="upload-ziele" hidden>
          <span class="upload-ziele-text" id="upload-ziele-text"></span>
          <span class="chat-head-fill"></span>
          <button class="ziel-btn" id="ziel-chat" type="button"><i data-icon="pfeil" data-icon-groesse="13"></i>In den Chat senden</button>
          <button class="ziel-btn is-primary" id="ziel-ingest" type="button"><i data-icon="hoch" data-icon-groesse="13"></i>Ingestieren</button>
        </div>

        <div class="files-log" id="files-log" hidden>
          <div class="files-log-head">
            <span>Ergebnis</span>
            <span class="files-log-close" id="files-log-close">&times;</span>
          </div>
          <pre id="files-log-body"></pre>
        </div>
      </div>
    </div>
  </div>
  <!-- ===================== VIEW: MONITOR =====================
       Vier Unterreiter statt einer langen Seite: jeder passt auf eine
       Bildschirmhoehe. Umgeschaltet wird nach demselben .active-Muster wie die
       Hauptansichten -- der Hash bleibt einsegmentig (#/monitor). -->
  <section class="view" id="view-monitor">

    <div class="subtabs" role="tablist">
      <button class="subtab active" data-tab="overview" role="tab">Überblick</button>
      <button class="subtab" data-tab="traces" role="tab">Traces</button>
      <button class="subtab" data-tab="accounts" role="tab">Konten</button>
    </div>

    <!-- ---------- Überblick ---------- -->
    <div class="subview active" id="tab-overview">

      <!-- Fuenf Kennzahlen auf einen Blick. Die Werte stammen aus /summary und
           stehen weiter unten ausfuehrlicher; hier zaehlt der schnelle Blick. -->
      <div class="kacheln">
        <div class="kachel">
          <div class="kachel-kopf">DURCHSATZ</div>
          <div class="kachel-zahl"><span id="k-durchsatz">&ndash;</span><i>token/s</i></div>
          <svg class="kachel-spark" id="k-spark" viewBox="0 0 120 26" preserveAspectRatio="none"></svg>
        </div>
        <div class="kachel">
          <div class="kachel-kopf">TTFT</div>
          <div class="kachel-zahl"><span id="k-ttft">&ndash;</span><i>s</i></div>
          <div class="kachel-sub" id="k-lauf"></div>
        </div>
        <div class="kachel">
          <div class="kachel-kopf">KV-CACHE</div>
          <div class="kachel-zahl"><span id="k-kv">&ndash;</span><i>%</i></div>
          <div class="kachel-balken"><i id="k-kv-bar"></i></div>
          <div class="kachel-sub" id="k-kv-sub"></div>
        </div>
        <div class="kachel">
          <div class="kachel-kopf">ANFRAGEN</div>
          <div class="kachel-zahl"><span id="k-req">&ndash;</span><i>gesamt</i></div>
          <div class="kachel-sub" id="k-req-sub"></div>
        </div>
        <div class="kachel">
          <div class="kachel-kopf">PREFIX-CACHE</div>
          <div class="kachel-zahl"><span id="k-cache">&ndash;</span><i>% treffer</i></div>
          <div class="kachel-balken"><i id="k-cache-bar"></i></div>
          <div class="kachel-sub" id="k-tts-sub"></div>
        </div>
      </div>

      <!-- Reihen statt Spalten: der Entwurf stellt SYSTEM, Auslastung und
           Werkzeuge nebeneinander, darunter Collection und Auditprotokoll ueber
           die volle Breite. Die frueheren Panels LLM SPEED, KV-CACHE METRIC und
           RAG & WEB-SEARCH sind entfallen -- ihre Zahlen stehen in den Kacheln. -->
      <div class="monitor-reihe">
        <div class="panel">
          <div class="panel-header-desc">
            <span>SYSTEM</span>
            <span>SGLANG &middot; LOKAL</span>
          </div>
          <!-- Vier Zeilen statt der frueheren Liste: Anfragen und Dokumentzahl
               stehen jetzt in den Kacheln bzw. im Collection-Kopf. -->
          <div class="sys-grid">
            <span class="sys-key">MODELL</span>
            <span class="sys-wert" id="model-name">&ndash;</span>
            <span class="sys-key">EMBEDDING</span>
            <span class="sys-wert sys-cyan" id="sys-embed">&ndash;</span>
            <span class="sys-key">SUCHE</span>
            <span class="sys-wert" id="sys-suche">&ndash;</span>
            <span class="sys-key">TTS</span>
            <span class="sys-wert" id="sys-tts">&ndash;</span>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header-desc">
            <span>AGENTEN-LAST</span>
            <span>LOAD FACTOR</span>
          </div>
          <div class="stats-list">
            <div class="stats-item">
              <div class="stats-label last-label">RAG-Node <span id="rag-load">&ndash;</span></div>
              <div class="progress-container"><div class="progress-fill" id="rag-load-bar"></div></div>
            </div>
            <div class="stats-item">
              <div class="stats-label last-label">Search-Node <span id="search-load">&ndash;</span></div>
              <div class="progress-container"><div class="progress-fill" id="search-load-bar"></div></div>
            </div>
            <div class="stats-item">
              <div class="stats-label last-label">Action-Node <span id="action-load">&ndash;</span></div>
              <div class="progress-container"><div class="progress-fill" id="action-load-bar"></div></div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header-desc">
            <span>WERKZEUG-AUFRUFE</span>
            <span>SEIT START</span>
          </div>
          <div class="tool-usage" id="tool-usage"></div>
          <div class="muted" id="toolUsageEmpty" style="font-size: 0.78rem;">Noch kein Werkzeug aufgerufen.</div>
        </div>
      </div>

        <!-- Collection: jede Datei mit Chunks, Groesse und Zeitpunkt. Geloescht wird
             der INDEX, nicht das Original im Arbeitsverzeichnis. -->
        <div class="panel">
          <div class="panel-header-desc">
            <span>COLLECTION DOCS</span>
            <span class="coll-zahl" id="coll-zahl"></span>
            <span class="chat-head-fill"></span>
            <button class="coll-btn" id="coll-ingest" type="button"
                    title="Alle Dateien aus dem Uploads-Ordner indexieren">INGEST STARTEN</button>
            <button class="coll-clear" id="coll-clear" type="button">COLLECTION LEEREN</button>
          </div>
          <div class="coll-list" id="docs-list-container">
            <div class="muted">Lade Dokumente&hellip;</div>
          </div>
        </div>

    <!-- Reihenfolge wie im Entwurf: das Auditprotokoll ueber die volle Breite,
         Evals und Missionen darunter nebeneinander. -->
    <div class="collapsible-panel">
      <div class="panel-header" id="audit-panel-header">
        <h2>Auditprotokoll <span class="panel-head-sub" id="audit-sub">ACTION-ENGINE</span></h2>
        <span class="toggle-icon is-zu" id="audit-panel-icon" data-icon="chevron" data-icon-groesse="11"></span>
      </div>
      <div class="panel-content collapsed" id="audit-panel">
        <div class="panel-toolbar">
          <button id="btnCopyAuditPath" class="eval-btn eval-btn-blue" data-path="C:\Argus_Workspace\Evals\Action_Audit"
                  title="Pfad in die Zwischenablage -- im Explorer mit Strg+V in die Adresszeile"><i data-icon="copy" data-icon-groesse="13"></i>Ordnerpfad kopieren</button>
          <button id="btnClearAudit" class="eval-btn eval-btn-magenta"><i data-icon="trash" data-icon-groesse="13"></i>Audit-DB leeren</button>
          <span class="muted" style="font-size: 0.78rem;">JSONL-Dateien bleiben in C:\Argus_Workspace\Evals\Action_Audit\ erhalten</span>
        </div>
        <table class="audit-tabelle"><thead><tr><th>ZEIT</th><th>WERKZEUG</th><th>KLASSE</th><th>STATUS</th><th>LATENZ</th></tr></thead><tbody id="audit"></tbody></table>
        <div class="muted" id="auditEmpty" style="margin-top:10px;">Auditprotokoll leer.</div>
      </div>
    </div>

    <div class="monitor-reihe">
    <div class="collapsible-panel">
      <div class="panel-header" id="evals-panel-header">
        <h2>Qualit&auml;tssicherungs-Eval-L&auml;ufe</h2>
        <span class="toggle-icon is-zu" id="evals-panel-icon" data-icon="chevron" data-icon-groesse="11"></span>
      </div>
      <div class="panel-content collapsed" id="evals-panel">
        <div class="panel-toolbar">
          <button id="btnOpenFolder" class="eval-btn eval-btn-blue"><i data-icon="folder" data-icon-groesse="13"></i>Run-Logs anzeigen</button>
          <button id="btnCopyEvalPath" class="eval-btn eval-btn-blue" data-path="C:\Argus_Workspace\Evals\Run_Logs"
                  title="Pfad in die Zwischenablage -- im Explorer mit Strg+V in die Adresszeile"><i data-icon="copy" data-icon-groesse="13"></i>Ordnerpfad kopieren</button>
          <button id="btnClearEvals" class="eval-btn eval-btn-magenta"><i data-icon="trash" data-icon-groesse="13"></i>Verlauf l&ouml;schen</button>
          <span class="muted" style="font-size: 0.78rem; margin-left: 8px;">C:\Argus_Workspace\Evals\Run_Logs\</span>
        </div>
        <table><thead><tr><th>Zeitstempel</th><th>Suite</th><th>Konfiguration</th><th>Metrik</th><th>Wert</th></tr></thead><tbody id="evals"></tbody></table>
        <div class="muted" id="evalsEmpty" style="margin-top:10px;">Keine Eval-Datens&auml;tze gefunden.</div>
        <div id="evalLogsBox" style="display:none; margin-top:14px;">
          <div class="muted" style="font-size:0.78rem; margin-bottom:6px;"><span id="evalLogsDir">/app/evals/Run_Logs</span></div>
          <div id="evalLogsList"></div>
          <pre id="evalLogContent" style="display:none; margin-top:10px; max-height:340px; overflow:auto; background:rgba(0,0,0,0.35); border:1px solid rgba(0,229,255,0.25); border-radius:8px; padding:12px; font-size:0.74rem; white-space:pre-wrap; word-break:break-word;"></pre>
        </div>
      </div>
    </div>
    <div class="collapsible-panel">
      <div class="panel-header" id="missions-panel-header">
        <h2>Cloud-Missionen (Gemini)</h2>
        <span class="toggle-icon is-zu" id="missions-panel-icon" data-icon="chevron" data-icon-groesse="11"></span>
      </div>
      <div class="panel-content collapsed" id="missions-panel">
        <div class="panel-toolbar">
          <button id="btnClearMissions" class="eval-btn eval-btn-magenta"><i data-icon="trash" data-icon-groesse="13"></i>Beendete l&ouml;schen</button>
          <span class="muted" style="font-size: 0.78rem;">Laufende Missionen bleiben stehen &mdash; erst abbrechen, dann l&ouml;schen. Zeile anklicken zeigt das Ergebnis.</span>
        </div>
        <table><thead><tr><th>ID</th><th>Status</th><th>Provider</th><th>Calls</th><th>Tokens ein/aus</th><th>Schritt</th><th>Ziel</th><th></th></tr></thead><tbody id="missions"></tbody></table>
        <div class="muted" id="missionsEmpty" style="margin-top:10px;">Keine Missionen vorhanden.</div>
      </div>
    </div>
    </div>

    <!-- ---------- Ablaufverfolgung ---------- -->
    </div>

    <div class="subview" id="tab-traces">
        <div class="panel traces-panel-live">
          <!-- Eine Zeile wie im Entwurf: Name, Quelle, Zahl, dann die beiden Knoepfe. -->
          <div class="traces-kopf">
            <span class="traces-titel">TRACES</span>
            <span class="traces-quelle" id="traces-quelle"><i></i>PHOENIX &middot; 127.0.0.1:6006 &middot; PROJEKT ARGUS</span>
            <span class="chat-head-fill"></span>
            <span class="traces-zahl" id="traces-zahl"></span>
            <button id="btnTracesReload" class="traces-btn" type="button" title="Trace-Ansicht neu laden"><i data-icon="reload" data-icon-groesse="12"></i>Neu laden</button>
            <a href="http://127.0.0.1:6006" target="_blank" rel="noopener"
               class="traces-btn is-primary"><i data-icon="extern" data-icon-groesse="12"></i>Eigener Tab</a>
          </div>
          <!-- src erst beim Betreten der Ansicht setzen (router.js): ein iframe mit
               src im DOM zieht die Phoenix-Oberflaeche sofort mit. -->
          <iframe id="traces-frame" data-src="http://127.0.0.1:6006/projects"
                  title="Phoenix Tracing"></iframe>
          <div class="muted" id="traces-hint" style="font-size:0.72rem; margin-top:8px;">
            Laedt beim Betreten dieser Ansicht. Bleibt die Flaeche leer, laeuft der
            phoenix-Container nicht (<code>docker compose up -d phoenix</code>).
          </div>
        </div>
    </div>

    <!-- ---------- Konten ---------- -->
    <div class="subview" id="tab-accounts">
        <div class="panel">
          <div class="panel-header-desc">
            <span>BENUTZERKONTEN</span>
            <span>ZUGANG &amp; ROLLEN</span>
          </div>
          <div class="users-list" id="users-list"><div class="muted">Lade&hellip;</div></div>
          <form class="users-add" id="users-add">
            <input class="users-input" id="user-new-name" placeholder="Benutzername"
                   autocapitalize="none" spellcheck="false" autocomplete="off">
            <input class="users-input" id="user-new-pass" type="password"
                   placeholder="Passwort (min. 8)" autocomplete="new-password">
            <select class="users-input users-role" id="user-new-role">
              <option value="chat">Nur Chat &amp; Sprache</option>
              <option value="admin">Administrator</option>
            </select>
            <button class="eval-btn eval-btn-blue" type="submit">Konto anlegen</button>
          </form>
          <div class="muted" id="users-msg" style="font-size:0.74rem;"></div>
        </div>
    </div>

  </section>
</div>
<script src="js/common.js"></script>
<script src="js/bg.js"></script>
<script src="js/orb.js"></script>
<script src="js/auth.js"></script>
<script src="js/voice.js"></script>
<script src="js/ops.js"></script>
<script src="js/files.js"></script>
<script src="js/chat.js"></script>
<script src="js/argus-orb.js"></script>
<script src="js/router.js"></script>
</body>
</html>
'''

ARGUS_CSS = r'''  /* Schriften liegen lokal unter static/fonts/ -- die Seite laedt nichts von
     fremden Servern. Quelle sind die Dateien in assets/fonts/, die main() beim
     Generieren herueberkopiert; fehlen sie, greifen die Fallback-Stacks unten.
     Nur die tatsaechlich benutzten Schnitte, kein Schnitt zu viel. */
  @font-face { font-family: 'Chakra Petch'; font-style: normal; font-weight: 500;
               font-display: swap; src: url('fonts/chakra-petch-500.woff2') format('woff2'); }
  @font-face { font-family: 'Chakra Petch'; font-style: normal; font-weight: 600;
               font-display: swap; src: url('fonts/chakra-petch-600.woff2') format('woff2'); }
  @font-face { font-family: 'Chakra Petch'; font-style: normal; font-weight: 700;
               font-display: swap; src: url('fonts/chakra-petch-700.woff2') format('woff2'); }
  @font-face { font-family: 'Barlow'; font-style: normal; font-weight: 400;
               font-display: swap; src: url('fonts/barlow-400.woff2') format('woff2'); }
  @font-face { font-family: 'Barlow'; font-style: normal; font-weight: 500;
               font-display: swap; src: url('fonts/barlow-500.woff2') format('woff2'); }
  @font-face { font-family: 'Barlow'; font-style: normal; font-weight: 600;
               font-display: swap; src: url('fonts/barlow-600.woff2') format('woff2'); }
  @font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400;
               font-display: swap; src: url('fonts/jetbrains-mono-400.woff2') format('woff2'); }
  @font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 500;
               font-display: swap; src: url('fonts/jetbrains-mono-500.woff2') format('woff2'); }
  @font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 700;
               font-display: swap; src: url('fonts/jetbrains-mono-700.woff2') format('woff2'); }

  :root {
    color-scheme: dark;
    --bg-color: #05060e;
    --card-bg: rgba(18, 20, 40, 0.42);
    --card-border: rgba(180, 130, 255, 0.12);
    --card-border-hover: rgba(255, 43, 214, 0.45);
    --color-blue: #15d6ff;
    --color-cyan: #00e5ff;
    --color-magenta: #ff2bd6;
    --color-purple: #a64bff;
    --color-green: #00ffa3;
    --text-primary: #f3edff;
    --text-secondary: #9b93c9;
    --font-display: 'Chakra Petch', 'Segoe UI Semibold', 'Trebuchet MS', sans-serif;
    --font-body: 'Barlow', 'Segoe UI', -apple-system, sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace;
    /* EINE Stellschraube fuer die Groesse der Chat-Ansicht: Schrift, Abstaende und
       die Breite des Unterhaltungsstrangs haengen daran. 1 = Entwurfsmasse. */
    --chat-scale: 1;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    font-family: var(--font-body);
    font-weight: 500;
    background-color: var(--bg-color);
    background-image: 
      linear-gradient(rgba(0, 68, 204, 0.01) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 68, 204, 0.01) 1px, transparent 1px);
    background-size: 30px 30px;
    color: var(--text-primary);
    min-height: 100vh;
    position: relative;
    overflow-x: hidden;
  }

  .bg-glow {
    position: fixed;
    width: 520px;
    height: 520px;
    border-radius: 50%;
    filter: blur(170px);
    opacity: 0.16;
    z-index: -1;
    pointer-events: none;
    animation: glow-drift 14s ease-in-out infinite;
  }
  .bg-glow-1 {
    top: -160px;
    left: -160px;
    background: var(--color-cyan);
  }
  .bg-glow-2 {
    bottom: -160px;
    right: -160px;
    background: var(--color-magenta);
    animation-delay: -7s;
  }
  .bg-glow-3 {
    top: 30%;
    left: 45%;
    width: 420px;
    height: 420px;
    background: var(--color-purple);
    opacity: 0.1;
    animation-delay: -3.5s;
  }

  body::after {
    content: " ";
    display: block;
    position: fixed;
    top: 0; left: 0; bottom: 0; right: 0;
    background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.12) 50%);
    z-index: 9999;
    background-size: 100% 4px;
    pointer-events: none;
    opacity: 0.2;
  }

  .dashboard-container {
    padding: 10px 12px;
    max-width: 100%;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 100vh;
    box-sizing: border-box;
  }

  /* Abstaende kommen aus dem gap der Kopfzeile, nicht aus Raendern -- sonst
     addieren sich beide. */
  .logo-area { display: flex; align-items: center; gap: 10px; }
  .logo-icon {
    display: flex;
    color: var(--color-cyan);
    filter: drop-shadow(0 0 7px rgba(0, 229, 255, 0.45));
    animation: logo-glow 3s ease-in-out infinite;
  }
  .logo-text h2 {
    font-family: var(--font-display);
    margin: 0;
    font-size: 15px;
    font-weight: 900;
    letter-spacing: 2px;
    background: linear-gradient(120deg, #fff 18%, var(--color-cyan) 42%, var(--color-magenta) 60%, #fff 82%);
    background-size: 200% auto;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: shine 4s linear infinite;
  }

  .system-status {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .pulse-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background-color: var(--color-green);
    box-shadow: 0 0 10px var(--color-green);
    animation: status-pulse 2s infinite;
  }
  .status-text {
    font-family: var(--font-mono);
    font-size: 13px;
    letter-spacing: 1px;
  }

  /* Der Ueberblick ist eine Folge von Reihen, keine Spaltenwand: die Kacheln oben,
     darunter drei schmale Panels nebeneinander, dann zwei Bloecke ueber die volle
     Breite und zuletzt zwei nebeneinander. */
  /* auto-fit wie bei den Kacheln: unter 300 px je Spalte bricht die Reihe von
     selbst um -- feste Spaltenzahlen brauchten sonst eigene Media-Queries. */
  .monitor-reihe {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 10px;
    align-items: start;
  }

  .traces-panel-live {
    display: flex;
    flex-direction: column;
    min-height: 620px;
    height: 100%;
  }
  .traces-panel-live #traces-frame {
    flex: 1 1 auto;
    width: 100%;
    min-height: 480px;
    border: 1px solid rgba(0, 229, 255, 0.2);
    border-radius: 8px;
    background: #0b0d18;
  }
  .traces-toolbar {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin: 6px 0 10px;
  }

  .panel-toolbar {
    margin-bottom: 15px;
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }

  .kv-compact-line {
    margin-top: 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    text-align: center;
    transition: color 0.4s ease, text-shadow 0.4s ease;
  }
  .kv-compact-line.has-compaction {
    color: var(--color-cyan);
  }
  .kv-compact-line.recent {
    color: var(--color-magenta);
    text-shadow: 0 0 10px rgba(255, 43, 214, 0.55);
    animation: kvCompactPulse 1.6s ease-in-out infinite;
  }
  @keyframes kvCompactPulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }

  .mission-del-btn {
    cursor: pointer;
    color: var(--color-magenta);
    font-weight: 700;
    padding: 0 6px;
    opacity: 0.75;
    transition: opacity 0.2s ease;
  }
  .mission-del-btn:hover { opacity: 1; }

  .panel {
    background: linear-gradient(135deg, rgba(42, 30, 72, 0.34), rgba(12, 14, 28, 0.55));
    border: 1px solid var(--card-border);
    backdrop-filter: blur(24px) saturate(165%);
    -webkit-backdrop-filter: blur(24px) saturate(165%);
    border-radius: 14px;
    padding: 12px;
    position: relative;
    overflow: hidden;
    box-shadow:
      0 8px 32px rgba(0, 0, 0, 0.45),
      inset 0 1px 0 rgba(255, 255, 255, 0.07),
      inset 0 0 26px rgba(166, 75, 255, 0.04);
    transition: border-color 0.3s ease, box-shadow 0.3s ease, transform 0.3s ease;
  }
  .panel:hover {
    border-color: var(--card-border-hover);
    box-shadow:
      0 10px 42px rgba(255, 43, 214, 0.13),
      inset 0 1px 0 rgba(255, 255, 255, 0.09),
      inset 0 0 32px rgba(255, 43, 214, 0.05);
    transform: translateY(-1px);
  }
  .panel::before {
    content: ''; position: absolute; top: 0; left: 0; width: 6px; height: 6px;
    border-top: 2px solid var(--color-blue); border-left: 2px solid var(--color-blue);
  }
  .panel::after {
    content: ''; position: absolute; bottom: 0; right: 0; width: 6px; height: 6px;
    border-bottom: 2px solid var(--color-magenta); border-right: 2px solid var(--color-magenta);
  }

  /* Name links, Herkunft rechts, keine Trennlinie -- die Karte selbst grenzt ab. */
  /* space-between traegt den Zweispalten-Fall (Name links, Herkunft rechts). Wo
     Knoepfe folgen, schluckt der .chat-head-fill den Rest zuerst. */
  .panel-header-desc {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 11px;
    font-family: var(--font-display);
    font-size: 11px;
    letter-spacing: 2.2px;
    text-transform: uppercase;
    color: #c3d2e2;
  }
  /* Alles ausser dem ersten Feld ist Beiwerk: kleiner, gedaempft, Monoschrift. */
  .panel-header-desc > span:not(:first-child) {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.4px;
    color: #4d5c70; text-transform: uppercase;
  }

  .stats-list {
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .stats-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stats-label {
    font-family: var(--font-mono);
    font-size: 15px;
    color: var(--text-secondary);
    display: flex;
    justify-content: space-between;
  }
  .stats-value {
    font-family: var(--font-body);
    font-size: 19px;
    font-weight: 600;
    color: #fff;
  }

  .progress-container {
    height: 6px;
    background: rgba(0, 68, 204, 0.08);
    border: 1px solid rgba(0, 68, 204, 0.15);
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--color-blue), var(--color-magenta));
    transition: width 0.5s ease;
  }

  .tool-usage { display: flex; flex-direction: column; gap: 8px; }
  .tool-usage:empty { display: none; }
  .tool-usage-row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 3px 10px;
    align-items: baseline;
  }
  .tool-usage-name {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tool-usage-count {
    font-family: var(--font-display);
    font-size: 12px;
    font-weight: 700;
    color: var(--color-cyan);
  }
  .tool-usage-row .progress-container { grid-column: 1 / -1; }

  /* ===== Live-Ansicht: Vollbild-Sprachmodus =====
     Dunkel und leer bis auf den Orb. Der Verlauf steht in der Chat-Ansicht --
     hier lenkt nichts vom Zuhoeren ab. */
  .live-stage {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: 1;
    min-height: calc(100vh - 92px);
    border-radius: 16px;
    overflow: hidden;
    background:
      radial-gradient(ellipse at 50% 45%, rgba(16, 12, 38, 0.55), rgba(2, 3, 8, 0.92) 70%);
    border: 1px solid rgba(180, 130, 255, 0.10);
  }

  /* Der Orb fuellt die Buehne. Explizite Prozente sind Pflicht: canvas ist ein
     replaced element und faellt bei auto auf seine Attribut-Groesse zurueck. */
  #galaxy-canvas {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    z-index: 1;
    pointer-events: none;
  }

  .live-state {
    position: absolute;
    top: 7%;
    z-index: 3;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 7px;
    font-family: var(--font-display);
    font-size: 13px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #7fd9ff;
    text-shadow: 0 0 14px rgba(0, 229, 255, 0.55);
    transition: color 0.4s ease, text-shadow 0.4s ease;
  }
  .live-state-wort { display: flex; align-items: center; gap: 9px; }
  /* Der Punkt traegt die Zustandsfarbe ueber currentColor mit. */
  .live-state-punkt {
    width: 5px; height: 5px; border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 8px currentColor;
    animation: status-pulse 2s ease-in-out infinite;
  }
  /* Zweite Zeile: was gerade passiert, damit der Zustand nicht geraten wird. */
  .live-state-sub {
    font-family: var(--font-mono);
    font-size: 8.5px;
    letter-spacing: 2.6px;
    color: var(--text-secondary);
    text-shadow: none;
  }
  .live-state.is-listening { color: #78ffcd; text-shadow: 0 0 14px rgba(120, 255, 205, 0.6); }
  .live-state.is-thinking  { color: #c89bff; text-shadow: 0 0 14px rgba(166, 75, 255, 0.6); }
  .live-state.is-speaking  { color: #7fd9ff; text-shadow: 0 0 16px rgba(0, 229, 255, 0.75); }
  .live-state.is-research  { color: #ffcc66; text-shadow: 0 0 14px rgba(255, 200, 80, 0.6); }

  /* Untertitel statt Verlauf: zeigt nur den laufenden Wortwechsel. */
  .live-caption {
    position: absolute;
    bottom: 15%;
    z-index: 3;
    width: min(760px, 84%);
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 10px;
    pointer-events: none;
  }
  .live-caption-user {
    font-family: var(--font-mono);
    font-size: 13px;
    color: #9fb4ff;
    opacity: 0;
    transition: opacity 0.35s ease;
    text-shadow: 0 2px 10px #000;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .live-caption-user.is-on { opacity: 0.85; }
  .live-caption-ai {
    font-family: var(--font-mono);
    font-size: 15px;
    letter-spacing: 0.4px;
    color: #cfe0ff;
    opacity: 0;
    transition: opacity 0.35s ease;
    text-shadow: 0 2px 12px #000;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .live-caption-ai.is-on { opacity: 1; }

  .live-controls {
    position: absolute;
    bottom: 7%;
    z-index: 4;
    display: flex;
    align-items: center;
    gap: 22px;
  }

  /* Mikro als Hauptbedienung: gross, mittig, mit Puls beim Aufnehmen. */
  .live-mic {
    position: relative;
    width: 74px;
    height: 74px;
    border-radius: 50%;
    cursor: pointer;
    border: 1px solid rgba(0, 229, 255, 0.45);
    background: radial-gradient(circle at 50% 40%, rgba(0, 229, 255, 0.18), rgba(9, 12, 24, 0.95));
    color: #dff3ff;
    font-size: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.2s ease, border-color 0.25s ease, box-shadow 0.25s ease;
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.55), 0 0 22px rgba(0, 229, 255, 0.16);
  }
  .live-mic:hover { transform: scale(1.05); box-shadow: 0 8px 34px rgba(0,0,0,0.6), 0 0 30px rgba(0, 229, 255, 0.34); }
  .live-mic-glyph { position: relative; z-index: 2; }
  .live-mic-ring {
    position: absolute;
    inset: -6px;
    border-radius: 50%;
    border: 1px solid rgba(255, 43, 214, 0.55);
    opacity: 0;
  }
  .live-mic.rec {
    border-color: var(--color-magenta);
    background: radial-gradient(circle at 50% 40%, rgba(255, 43, 214, 0.28), rgba(24, 6, 20, 0.95));
    box-shadow: 0 8px 34px rgba(0, 0, 0, 0.6), 0 0 34px rgba(255, 43, 214, 0.45);
  }
  .live-mic.rec .live-mic-ring { animation: mic-pulse 1.5s ease-out infinite; }
  @keyframes mic-pulse {
    0%   { opacity: 0.8; transform: scale(1); }
    100% { opacity: 0;   transform: scale(1.45); }
  }
  /* Lautstaerke-Kranz: --level wird von voice.js aus dem gemessenen Tonpegel
     gesetzt. Er macht sichtbar, dass das Mikro tatsaechlich etwas hoert. */
  .live-mic::after {
    content: '';
    position: absolute;
    inset: -4px;
    border-radius: 50%;
    border: 2px solid rgba(0, 229, 255, 0.65);
    opacity: calc(var(--level, 0) * 0.9);
    transform: scale(calc(1 + var(--level, 0) * 0.35));
    transition: opacity 0.08s linear, transform 0.08s linear;
    pointer-events: none;
  }
  .live-mic.rec::after { border-color: rgba(255, 43, 214, 0.75); }

  .live-btn {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    cursor: pointer;
    border: 1px solid rgba(180, 130, 255, 0.22);
    background: rgba(10, 12, 24, 0.75);
    color: #b9c6e4;
    font-size: 17px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
  }
  .live-btn:hover { border-color: var(--color-blue); color: #fff; background: rgba(0, 229, 255, 0.1); }
  .live-btn.off { opacity: 0.4; }
  .live-btn.is-stop {
    border-color: var(--color-magenta);
    color: var(--color-magenta);
    background: rgba(255, 43, 214, 0.14);
  }

  .live-typebar {
    position: absolute;
    bottom: calc(7% + 92px);
    z-index: 4;
    display: flex;
    gap: 10px;
    align-items: center;
    width: min(620px, 84%);
  }
  .live-typebar[hidden] { display: none; }
  #vc-text {
    flex: 1;
    padding: 12px 16px;
    border-radius: 24px;
    border: 1px solid rgba(180, 130, 255, 0.25);
    background: rgba(8, 10, 20, 0.92);
    color: #e6eefc;
    font-size: 16px;
    font-family: var(--font-body);
    outline: none;
  }
  #vc-text:focus { border-color: var(--color-blue); box-shadow: 0 0 0 3px rgba(0, 229, 255, 0.1); }

  .live-hint {
    position: absolute;
    bottom: 2.2%;
    z-index: 3;
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 1px;
    color: var(--text-secondary);
    opacity: 0.55;
  }

  @media (max-height: 720px) {
    .live-caption { bottom: 18%; }
    .live-caption-ai { font-size: 13px; }
    .live-mic { width: 62px; height: 62px; font-size: 22px; }
  }

  .big-stat-panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 20px;
  }

  .hud-metrics-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 24px;
    width: 100%;
    margin-top: 15px;
    padding-top: 15px;
    border-top: 1px solid rgba(180, 130, 255, 0.15);
  }
  .metric-block {
    background: rgba(18, 20, 40, 0.25);
    border: 1px solid rgba(180, 130, 255, 0.08);
    border-radius: 10px;
    padding: 18px;
    transition: border-color 0.3s ease;
  }
  .metric-block:hover {
    border-color: rgba(255, 43, 214, 0.25);
  }
  .metric-block-title {
    font-family: var(--font-display);
    font-size: 12px;
    color: var(--color-blue);
    font-weight: 700;
    letter-spacing: 1.5px;
    margin-bottom: 12px;
    text-transform: uppercase;
    border-bottom: 1px solid rgba(0, 229, 255, 0.1);
    padding-bottom: 4px;
  }
  .metric-row {
    display: flex;
    justify-content: space-between;
    font-family: var(--font-mono);
    font-size: 13px;
    margin-bottom: 9px;
  }
  .metric-row:last-child {
    margin-bottom: 0;
  }
  .metric-label {
    color: var(--text-secondary);
  }
  .metric-value {
    color: #fff;
    font-weight: 500;
  }

  .doc-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 6px 8px;
    background: rgba(180, 130, 255, 0.04);
    border: 1px solid rgba(180, 130, 255, 0.08);
    border-radius: 4px;
    gap: 10px;
  }
  .doc-name {
    color: #fff;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 180px;
  }
  .doc-meta {
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .doc-delete-btn {
    color: var(--color-magenta);
    cursor: pointer;
    font-weight: bold;
    font-size: 12px;
    padding: 0 4px;
    transition: transform 0.2s ease, text-shadow 0.2s ease;
  }
  .doc-delete-btn:hover {
    transform: scale(1.2);
    text-shadow: 0 0 5px var(--color-magenta);
  }
  .big-stat-val {
    font-family: var(--font-display);
    font-size: 36px;
    font-weight: 700;
    color: #fff;
    text-shadow: 0 0 12px rgba(0, 229, 255, 0.45), 0 0 22px rgba(255, 43, 214, 0.25);
  }
  .big-stat-label {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .sparkline-box {
    height: 60px;
    border-bottom: 1px solid rgba(0, 68, 204, 0.15);
    margin-bottom: 15px;
  }

  .collapsible-panel {
    background: linear-gradient(135deg, rgba(42, 30, 72, 0.34), rgba(12, 14, 28, 0.55));
    border: 1px solid var(--card-border);
    backdrop-filter: blur(24px) saturate(165%);
    -webkit-backdrop-filter: blur(24px) saturate(165%);
    border-radius: 14px;
    box-shadow:
      0 8px 32px rgba(0, 0, 0, 0.45),
      inset 0 1px 0 rgba(255, 255, 255, 0.07);
    padding: 0;
    overflow: hidden;
    position: relative;
    transition: border-color 0.3s ease;
  }
  .collapsible-panel:hover {
    border-color: var(--card-border-hover);
  }
  .collapsible-panel::before {
    content: ''; position: absolute; top: 0; left: 0; width: 6px; height: 6px;
    border-top: 2px solid var(--color-blue); border-left: 2px solid var(--color-blue);
  }
  .collapsible-panel::after {
    content: ''; position: absolute; bottom: 0; right: 0; width: 6px; height: 6px;
    border-bottom: 2px solid var(--color-magenta); border-right: 2px solid var(--color-magenta);
  }
  .panel-header {
    padding: 16px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    background: rgba(0, 68, 204, 0.02);
    user-select: none;
    transition: background 0.3s ease;
  }
  .panel-header:hover {
    background: rgba(0, 68, 204, 0.06);
  }
  .panel-header h2 {
    font-family: var(--font-display);
    font-size: 13px;
    font-weight: 700;
    margin: 0;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 1.5px;
  }
  /* Zugeklappt zeigt das Chevron nach unten, aufgeklappt nach oben. Gedreht statt
     getauscht -- ein zweites Zeichen haette eine andere Breite. */
  .toggle-icon {
    display: flex;
    color: var(--color-blue);
    transform: rotate(180deg);
    transition: transform 0.2s ease;
  }
  .toggle-icon.is-zu { transform: rotate(0deg); }
  /* Dasselbe an aufklappbaren Tabellenzeilen, nur kleiner und in der Zeile liegend. */
  .expand-chevron {
    display: inline-flex; vertical-align: middle; margin-left: 6px;
    color: var(--color-blue); opacity: 0.7;
    transition: transform 0.2s ease;
  }
  .expand-chevron.is-auf { transform: rotate(180deg); }
  .panel-content {
    padding: 20px;
    border-top: 1px solid rgba(0, 68, 204, 0.15);
    max-height: 400px;
    overflow-y: auto;
  }
  .panel-content.collapsed {
    display: none;
  }

  /* Werte in Zeilen wie im Entwurf: kleine Monoschrift, Kopfzeile noch kleiner und
     gedaempft, keine Trennlinien zwischen den Zeilen. */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 5px 7px; border: none; }
  th {
    font-family: var(--font-mono); font-size: 8.5px; font-weight: 400;
    letter-spacing: 1.4px; text-transform: uppercase; color: #4d5c70;
    padding-bottom: 8px;
  }
  td { font-family: var(--font-mono); font-size: 10.5px; color: #7e91a8; }
  /* Die letzte Spalte ist immer die Dauer -- rechtsbuendig laesst sich die
     Groessenordnung auf einen Blick vergleichen. */
  th:last-child, td:last-child { text-align: right; }
  tr:hover td { background: rgba(146, 178, 224, 0.035); }
  /* Spaltenbreiten aus dem Entwurf. Werkzeug nimmt den Rest. */
  .audit-tabelle { table-layout: fixed; }
  .audit-tabelle th:nth-child(1), .audit-tabelle td:nth-child(1) { width: 118px; }
  .audit-tabelle th:nth-child(3), .audit-tabelle td:nth-child(3) { width: 92px; }
  .audit-tabelle th:nth-child(4), .audit-tabelle td:nth-child(4) { width: 118px; }
  .audit-tabelle th:nth-child(5), .audit-tabelle td:nth-child(5) { width: 64px; }
  .audit-tabelle td:nth-child(2) {
    color: #dfe9f3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  .sys-online {
    color: var(--color-green);
    font-family: var(--font-mono);
    text-transform: uppercase;
  }
  .err-blink {
    color: var(--color-magenta);
    animation: blink-red 1.5s infinite;
    font-family: var(--font-mono);
    font-weight: bold;
    text-transform: uppercase;
  }
  .status-indicator {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-right: 8px;
    vertical-align: middle;
  }
  .status-indicator.online {
    background: var(--color-green);
    box-shadow: 0 0 6px var(--color-green);
  }
  .status-indicator.offline {
    background: var(--color-magenta);
    box-shadow: 0 0 6px var(--color-magenta);
  }

  /* ===== Verbindungszustand ===== */
  .nav-status.is-offline .pulse-dot {
    background-color: var(--color-magenta);
    box-shadow: 0 0 10px var(--color-magenta);
    animation: blink-red 1.5s infinite;
  }
  .nav-status.is-offline .status-text {
    color: var(--color-magenta);
    font-weight: 700;
  }

  /* Eingefrorene Werte ausgrauen statt leeren: der letzte Stand bleibt nuetzlich,
     darf aber nicht wie eine Live-Zahl aussehen. */
  #view-monitor.is-stale .panel,
  #view-monitor.is-stale .collapsible-panel {
    opacity: 0.4;
    filter: saturate(0.3);
    transition: opacity 0.4s ease, filter 0.4s ease;
  }
  /* Phoenix laeuft im eigenen Container: ein totes rag-backend sagt ueber die
     Trace-Ansicht nichts aus. */
  #view-monitor.is-stale .traces-panel-live {
    opacity: 1;
    filter: none;
  }

  .muted { color: var(--text-secondary); font-size: 15px; font-family: var(--font-mono); }

  @keyframes rotate-gradient {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }
  @keyframes shine {
    to { background-position: 200% center; }
  }
  @keyframes logo-glow {
    0%, 100% { box-shadow: 0 0 12px rgba(0, 229, 255, 0.35), 0 0 22px rgba(255, 43, 214, 0.2); }
    50%      { box-shadow: 0 0 18px rgba(0, 229, 255, 0.65), 0 0 36px rgba(255, 43, 214, 0.45); }
  }
  @keyframes argus-flicker {
    0%, 17%, 21%, 59%, 63%, 100% { opacity: 1; }
    19%, 61% { opacity: 0.74; }
    20% { opacity: 0.92; }
    80%, 82% { opacity: 0.85; }
  }
  @keyframes glow-drift {
    0%, 100% { transform: translate(0, 0) scale(1); }
    50%      { transform: translate(40px, 30px) scale(1.12); }
  }
  @keyframes breathe-pulse {
    0%, 100% { transform: scale(1); opacity: 0.8; }
    50% { transform: scale(1.04); opacity: 1; fill: rgba(0, 68, 204, 0.15); }
  }
  @keyframes core-breathe {
    0%, 100% { box-shadow: 0 0 8px rgba(0, 68, 204, 0.3); }
    50% { box-shadow: 0 0 15px rgba(179, 0, 89, 0.6); border-color: var(--color-magenta); }
  }
  @keyframes status-pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }
  @keyframes blink-red {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  .eval-btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 12px;
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.3s ease;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .eval-btn i { display: flex; }
  .eval-btn-blue {
    border: 1px solid var(--color-blue);
    background: rgba(0, 229, 255, 0.1);
    color: var(--color-blue);
  }
  .eval-btn-blue:hover {
    background: var(--color-blue);
    color: #001018;
    box-shadow: 0 0 8px var(--color-blue);
  }
  .eval-btn-magenta {
    border: 1px solid var(--color-magenta);
    background: rgba(255, 43, 214, 0.1);
    color: var(--color-magenta);
  }
  .eval-btn-magenta:hover {
    background: var(--color-magenta);
    color: #001018;
    box-shadow: 0 0 8px var(--color-magenta);
  }

  /* ===== Navigation & Views (Mehrseiten-Shell) ===== */
  .argus-nav {
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 10px 16px;
    background: linear-gradient(135deg, rgba(40, 30, 70, 0.35), rgba(12, 14, 28, 0.55));
    border: 1px solid var(--card-border);
    backdrop-filter: blur(22px) saturate(160%);
    -webkit-backdrop-filter: blur(22px) saturate(160%);
    border-radius: 10px;
    position: relative;
    overflow: hidden;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.06);
  }
  .argus-nav::before {
    content: ''; position: absolute; top: 0; left: 0; width: 8px; height: 100%;
    background: linear-gradient(180deg, var(--color-cyan), var(--color-magenta));
    box-shadow: 0 0 14px rgba(255, 43, 214, 0.5);
    border-top-left-radius: 10px; border-bottom-left-radius: 10px;
  }
  .nav-fill { flex: 1 1 auto; }
  .nav-links { display: flex; align-items: center; gap: 4px; }
  .nav-link {
    font-family: var(--font-display);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 2.2px;
    text-transform: uppercase;
    color: var(--text-secondary);
    text-decoration: none;
    padding: 7px 15px;
    border: 1px solid transparent;
    border-radius: 10px;
    transition: color 0.25s ease, border-color 0.25s ease, background 0.25s ease;
  }
  .nav-link:hover { color: var(--text-primary); background: rgba(0, 229, 255, 0.06); }
  .nav-link.active {
    color: #fff;
    border-color: var(--color-blue);
    background: rgba(0, 229, 255, 0.1);
    box-shadow: 0 0 10px rgba(0, 229, 255, 0.25), inset 0 0 12px rgba(0, 229, 255, 0.05);
  }
  .nav-status {
    display: flex; align-items: center; gap: 7px; flex-shrink: 0; white-space: nowrap;
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 1.8px;
  }

  /* Views werden umgeschaltet, nicht neu gebaut: der WebGL-Orb behaelt seinen
     Kontext, der Chat-Verlauf ueberlebt den Wechsel. */
  .view { display: none; }
  .view.active {
    display: flex; flex-direction: column; gap: 10px;
    /* 88px = Kopfzeile plus die beiden Aussenabstaende. Aus dem Entwurf. */
    min-height: calc(100vh - 88px);
  }
  /* Nur die Chat-Ansicht fuellt die Resthoehe -- die Monitor-Ansicht ist hoeher
     als der Bildschirm und wuerde von flex:1 gestaucht statt zu scrollen. */
  #view-chat.active { flex: 1; min-height: 0; }

  /* Ziehen von Dateien in den Chat. Der Rahmen liegt auf der ganzen Ansicht, damit
     man nicht ein kleines Ziel treffen muss; das Overlay faengt keine Ereignisse ab
     (pointer-events: none), sonst wuerde das drop nie beim Container ankommen. */
  #view-chat.is-drop-over { position: relative; }
  #view-chat.is-drop-over::after {
    content: "DATEI HIER ABLEGEN";
    position: absolute;
    inset: 6px;
    z-index: 40;
    pointer-events: none;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1.5px dashed var(--color-magenta);
    border-radius: 14px;
    background: rgba(255, 43, 214, 0.07);
    font-family: var(--font-display);
    font-size: 13px;
    letter-spacing: 2px;
    color: var(--color-magenta);
    text-shadow: 0 0 12px rgba(255, 43, 214, 0.5);
  }


  /* ===== Chat-Ansicht ===== */
  /* Zwei getrennte Flaechen statt einer geteilten: der Entwurf setzt Verlauf und
     Unterhaltung als eigene Karten nebeneinander, nicht durch eine Linie geteilt.
     Die Huelle selbst traegt deshalb keinen Rand und keinen Grund. */
  .chat-shell {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    gap: 10px;
    padding: 0;
    background: none;
    border: none;
    box-shadow: none;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
    flex: 1;
    min-height: 0;
  }
  .chat-sidebar {
    display: flex; flex-direction: column; gap: 9px;
    padding: 12px;
    min-height: 0;
  }
  .chat-new-btn {
    font-family: var(--font-mono); font-size: 13px; letter-spacing: 1px;
    padding: 9px 12px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--color-blue); background: rgba(0, 229, 255, 0.1);
    color: var(--color-blue); transition: all 0.25s ease;
  }
  .chat-new-btn:hover { background: var(--color-blue); color: #001018; box-shadow: 0 0 10px var(--color-blue); }
  .chat-list { overflow-y: auto; display: flex; flex-direction: column; gap: 4px; min-height: 0; }
  .chat-item {
    display: flex; align-items: center; gap: 6px;
    padding: 8px 10px; border-radius: 7px; cursor: pointer;
    border: 1px solid transparent; transition: background 0.2s ease, border-color 0.2s ease;
  }
  .chat-item:hover { background: rgba(0, 229, 255, 0.05); }
  .chat-item.active { background: rgba(0, 229, 255, 0.1); border-color: rgba(0, 229, 255, 0.35); }
  .chat-item-title {
    flex: 1; min-width: 0; font-size: 13.5px; color: var(--text-primary);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .chat-item-del {
    color: var(--text-secondary); font-size: 15px; padding: 0 3px; opacity: 0;
    transition: opacity 0.2s ease, color 0.2s ease;
  }
  .chat-item:hover .chat-item-del { opacity: 1; }
  .chat-item-del:hover { color: var(--color-magenta); }

  /* Drei Zeilen: Kopf, Verlauf, Eingabe. overflow: hidden, damit die abgerundeten
     Ecken der Karte den Inhalt beschneiden. */
  .chat-main {
    display: grid; grid-template-rows: auto minmax(0, 1fr) auto;
    min-height: 0; min-width: 0; overflow: hidden;
  }
  .chat-head {
    display: flex; align-items: baseline; gap: 14px;
    padding: 14px 20px; border-bottom: 1px solid rgba(163, 198, 255, 0.08);
    font-family: var(--font-display); font-weight: 600; font-size: 14px;
    color: #eaf2fa;
  }
  .chat-messages {
    overflow-y: auto;
    padding: calc(22px * var(--chat-scale)) calc(20px * var(--chat-scale));
    display: flex; flex-direction: column; gap: calc(24px * var(--chat-scale));
    min-height: 0;
    scrollbar-width: thin; scrollbar-color: rgba(79, 216, 245, 0.35) transparent;
  }
  /* Der Antwortblock ist im Entwurf 820 px breit und linksbuendig, die Frage
     rechtsbuendig -- kein gemeinsam zentrierter Strang. */
  .chat-msg {
    display: flex; flex-direction: column; gap: calc(11px * var(--chat-scale));
    width: 100%; max-width: calc(820px * var(--chat-scale));
    position: relative;
  }
  .chat-msg.user { align-items: flex-end; max-width: 100%; }
  .chat-role {
    display: flex; align-items: center; gap: 10px;
    font-family: var(--font-mono); font-size: calc(9.5px * var(--chat-scale));
    letter-spacing: 2.4px;
    text-transform: uppercase; color: #7c92ad;
  }
  .chat-bubble {
    line-height: 1.65;
    font-size: calc(15.5px * var(--chat-scale));
    color: #dfe9f3; word-wrap: break-word; overflow-wrap: anywhere;
  }
  /* Nur die Frage ist eine Blase. Die Antwort steht frei im Block -- die untere
     rechte Ecke bleibt eckig und zeigt zum Absender. */
  .chat-msg.user .chat-bubble {
    max-width: 66%;
    padding: calc(11px * var(--chat-scale)) calc(15px * var(--chat-scale));
    border-radius: 14px 14px 4px 14px;
    border: 1px solid rgba(79, 216, 245, 0.24);
    background: linear-gradient(180deg, rgba(79, 216, 245, 0.11), rgba(79, 216, 245, 0.05));
    font-size: calc(14.5px * var(--chat-scale)); line-height: 1.55;
    color: #e4f4fd;
  }

  /* Ohne Nachrichten-ID bleibt die Aktionsleiste aus: ein Knopf, der nur das DOM
     aendert, waere eine Luege. */
  .chat-actions {
    display: flex; gap: 2px; margin-top: 2px;
    opacity: 0; transition: opacity 0.15s ease;
  }
  .chat-msg.user .chat-actions { justify-content: flex-end; }
  .chat-msg:hover .chat-actions { opacity: 1; }
  .chat-msg:not(.has-id) .chat-actions { display: none; }
  .chat-action {
    background: none; border: none; cursor: pointer; padding: 3px 6px;
    border-radius: 5px; color: var(--text-secondary);
    font-size: calc(13px * var(--chat-scale)); line-height: 1;
    transition: color 0.15s ease, background 0.15s ease;
  }
  .chat-action:hover { color: var(--color-blue); background: rgba(0, 229, 255, 0.1); }
  .chat-action:last-child:hover { color: var(--color-magenta); background: rgba(255, 43, 214, 0.1); }
  /* Die Antwort steht ohne Kasten. Farbe und Rand der Frage stehen weiter oben. */
  .chat-msg.ai .chat-bubble { background: none; border: none; }
  .chat-bubble p { margin: 0 0 10px; }
  .chat-bubble p:last-child { margin-bottom: 0; }
  .chat-bubble ul, .chat-bubble ol { margin: 0 0 10px; padding-left: 22px; }
  .chat-bubble li { margin-bottom: 4px; }
  .chat-bubble h1, .chat-bubble h2, .chat-bubble h3 {
    font-family: var(--font-display); margin: 14px 0 8px; font-size: calc(15px * var(--chat-scale));
    color: var(--color-cyan); letter-spacing: 0.5px;
  }
  .chat-bubble code {
    font-family: var(--font-mono); font-size: calc(13.5px * var(--chat-scale));
    background: rgba(0, 0, 0, 0.4); padding: 2px 5px; border-radius: 4px;
    color: #9ff0ff;
  }
  .chat-bubble a { color: var(--color-cyan); }
  /* Tabellen: das Modell liefert bei jedem Vergleich eine, vorher standen die
     rohen Pipe-Zeichen im Chat. Waagerecht scrollbar statt umbrechend, sonst
     sprengt eine breite Kostentabelle die Blase. */
  .chat-tabelle {
    margin: 12px 0; overflow-x: auto; border-radius: 8px;
    border: 1px solid rgba(0, 229, 255, 0.18); background: rgba(0, 0, 0, 0.3);
  }
  .chat-bubble table {
    border-collapse: collapse; width: 100%;
    font-size: calc(13.5px * var(--chat-scale));
  }
  .chat-bubble th, .chat-bubble td {
    padding: 7px 12px; text-align: left; vertical-align: top;
    border-bottom: 1px solid rgba(0, 229, 255, 0.10);
  }
  .chat-bubble th {
    font-family: var(--font-display); font-weight: 600; letter-spacing: 0.4px;
    color: var(--color-cyan); background: rgba(0, 229, 255, 0.06);
    border-bottom: 1px solid rgba(0, 229, 255, 0.22); white-space: nowrap;
  }
  .chat-bubble tbody tr:last-child td { border-bottom: none; }
  .chat-bubble tbody tr:hover td { background: rgba(0, 229, 255, 0.04); }
  /* Zahlenspalten rechtsbuendig, wenn die Trennzeile es vorgibt. */
  .chat-bubble td.rechts, .chat-bubble th.rechts { text-align: right; }
  .chat-bubble td.mitte,  .chat-bubble th.mitte  { text-align: center; }
  .chat-code {
    position: relative; margin: 10px 0; border-radius: 8px; overflow: hidden;
    border: 1px solid rgba(0, 229, 255, 0.2); background: rgba(0, 0, 0, 0.42);
  }
  .chat-code-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 10px; font-family: var(--font-mono); font-size: 11px;
    color: var(--text-secondary); border-bottom: 1px solid rgba(0, 229, 255, 0.12);
  }
  .chat-code pre { margin: 0; padding: 11px 13px; overflow-x: auto; }
  .chat-code code { background: none; padding: 0; font-size: 13px; line-height: 1.5; }
  .chat-copy { cursor: pointer; color: var(--color-blue); }
  .chat-copy:hover { text-shadow: 0 0 6px var(--color-blue); }

  .chat-tools { display: flex; flex-direction: column; gap: 3px; }

  /* Quellen ueber der Antwort, waagerecht scrollbar statt umbrechend --
     sonst schiebt eine lange Liste die Antwort aus dem Bild. */
  .chat-sources {
    display: flex; gap: 8px; overflow-x: auto; padding: 2px 0 8px;
    scrollbar-width: thin;
  }
  .src-card {
    flex: 0 0 auto; width: 182px; padding: 9px 11px; border-radius: 10px;
    background: rgba(120, 160, 255, 0.06);
    border: 1px solid rgba(140, 170, 255, 0.16);
    text-decoration: none; transition: border-color 0.15s ease, background 0.15s ease;
  }
  .src-card:hover { background: rgba(120, 160, 255, 0.11); border-color: rgba(160, 190, 255, 0.34); }
  .src-title {
    font-size: calc(11.5px * var(--chat-scale)); line-height: 1.32;
    color: var(--text-primary);
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .src-foot {
    margin-top: 7px; display: flex; align-items: center; gap: 6px;
    font-family: var(--font-mono); font-size: 10.5px; color: var(--text-secondary);
  }
  /* Monogramm statt Favicon: ein echtes Favicon laege auf einem fremden Server,
     die CSP erlaubt Bilder nur von 'self', und der Abruf verriete die Lektuere. */
  .src-mono {
    width: 15px; height: 15px; border-radius: 4px; flex: 0 0 auto;
    display: flex; align-items: center; justify-content: center;
    background: rgba(140, 180, 255, 0.20); color: #dce8ff; font-size: 9px;
  }

  .cite {
    display: inline-block; min-width: 15px; padding: 0 4px; margin: 0 1px;
    border-radius: 4px; cursor: pointer; vertical-align: 1px;
    font-family: var(--font-mono); font-size: 0.72em; line-height: 1.5;
    background: rgba(140, 180, 255, 0.16); color: #b9d0ff;
    border: 1px solid rgba(140, 180, 255, 0.22);
  }
  .cite:hover, .cite:focus-visible {
    background: rgba(140, 180, 255, 0.30); color: #eaf2ff; outline: none;
  }
  .cite.is-tot {
    background: rgba(255, 255, 255, 0.05); color: var(--text-secondary);
    border-color: rgba(255, 255, 255, 0.10); cursor: default;
  }
  .cite.is-tot:hover { background: rgba(255, 255, 255, 0.05); color: var(--text-secondary); }

  /* ---- Traces ---- */
  .traces-kopf { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .traces-titel {
    font-family: var(--font-display); font-size: 11px; letter-spacing: 2.2px;
    color: #c3d2e2;
  }
  .traces-quelle, .traces-zahl {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.4px;
    color: var(--text-secondary);
  }
  .traces-quelle { display: flex; align-items: center; gap: 7px; }
  .traces-quelle i {
    width: 5px; height: 5px; border-radius: 50%; background: var(--color-green);
    box-shadow: 0 0 8px var(--color-green);
  }
  .traces-quelle.ist-weg i { background: var(--color-magenta); box-shadow: none; }
  .traces-zahl:empty { display: none; }
  .traces-btn {
    display: flex; align-items: center; gap: 7px; flex-shrink: 0;
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.4px;
    text-transform: uppercase; text-decoration: none;
    padding: 6px 10px; border-radius: 8px; cursor: pointer;
    border: 1px solid rgba(163, 198, 255, 0.14);
    background: transparent; color: #8b9db2;
    transition: border-color 0.2s ease, color 0.2s ease;
  }
  .traces-btn:hover { border-color: rgba(79, 216, 245, 0.5); color: #fff; }
  .traces-btn.is-primary {
    border-color: rgba(79, 216, 245, 0.32);
    background: rgba(79, 216, 245, 0.10); color: #d6f4ff;
  }
  .traces-btn.is-primary:hover { border-color: #4fd8f5; }

  /* ---- Anhang-Dialog ---- */
  .dropzone-btn {
    margin-top: 4px; padding: 8px 14px; border-radius: 9px; cursor: pointer;
    border: 1px solid rgba(79, 216, 245, 0.34); background: rgba(79, 216, 245, 0.12);
    color: var(--color-cyan); font-family: var(--font-display); font-weight: 600;
    font-size: 11px; letter-spacing: 1.8px; text-transform: uppercase;
  }
  .dropzone-btn:hover { border-color: var(--color-cyan); color: #fff; }

  /* Eine Zeile je wartende Datei: Symbol, Name, Groesse, Zustand, Wegnehmen. */
  .upload-liste { display: flex; flex-direction: column; gap: 3px; }
  .upload-liste:empty { display: none; }
  .upload-zeile {
    display: grid; grid-template-columns: 18px minmax(0, 1fr) 74px 92px 26px;
    gap: 12px; align-items: center; padding: 8px 10px; border-radius: 10px;
    border: 1px solid var(--card-border); background: rgba(6, 10, 18, 0.4);
  }
  .upload-zeile i { display: flex; color: var(--text-secondary); }
  .upload-zeile-name {
    font-family: var(--font-mono); font-size: 10.5px; color: var(--text-primary);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .upload-zeile-groesse { font-family: var(--font-mono); font-size: 9.5px; color: var(--text-secondary); }
  .upload-zeile-zustand {
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1px;
    color: var(--color-green);
  }
  .upload-zeile-weg {
    width: 26px; height: 26px; border-radius: 8px; cursor: pointer;
    border: 1px solid transparent; background: none; color: var(--text-secondary);
    display: grid; place-items: center;
  }
  .upload-zeile-weg:hover {
    border-color: rgba(255, 110, 140, 0.4); color: var(--color-magenta);
    background: rgba(255, 110, 140, 0.08);
  }

  /* ---- Anmeldung ---- */
  .auth-logo { display: flex; justify-content: center; color: var(--color-cyan); }
  .auth-feld {
    display: flex; align-items: center; gap: 9px; padding: 0 12px;
    border-radius: 11px; border: 1px solid var(--card-border);
    background: rgba(6, 10, 18, 0.55);
  }
  .auth-feld:focus-within { border-color: var(--color-cyan); }
  .auth-feld-icon { display: flex; color: var(--text-secondary); flex: 0 0 auto; }
  .auth-feld .auth-input {
    flex: 1; min-width: 0; border: 0; background: none; padding: 11px 0;
    border-radius: 0;
  }
  .auth-feld .auth-input:focus { border: 0; box-shadow: none; }
  .auth-btn { display: flex; align-items: center; justify-content: center; gap: 9px; }
  .auth-btn i { display: flex; }
  .auth-fuss {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; padding-top: 14px; margin-top: 2px;
    border-top: 1px solid var(--card-border);
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.4px;
  }
  .auth-dienst { display: flex; align-items: center; gap: 7px; color: var(--text-secondary); }
  .auth-dienst i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
  .auth-dienst.ist-da { color: var(--color-green); }
  .auth-dienst.ist-weg { color: var(--color-magenta); }
  .auth-stack { color: var(--text-secondary); opacity: 0.75; }

  /* ---- SYSTEM-Panel: Schluessel links, Wert rechts ---- */
  .sys-grid {
    display: grid; grid-template-columns: auto minmax(0, 1fr);
    gap: 8px 16px; align-items: baseline;
  }
  .sys-key {
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.4px;
    color: var(--text-secondary);
  }
  .sys-wert {
    font-family: var(--font-body); font-weight: 600; font-size: 13px;
    color: var(--text-primary); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Knotenname und Prozentwert auf einer Zeile, wie im Entwurf. */
  .panel-head-sub {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.4px;
    color: var(--text-secondary); margin-left: 10px; font-weight: 400;
  }

  .last-label { font-size: 10.5px; letter-spacing: 0.4px; }
  .last-label span { color: var(--color-cyan); }

  .sys-cyan { color: var(--color-cyan); }
  .sys-gruen { color: var(--color-green); }
  .sys-magenta { color: var(--color-magenta); }

  /* ---- Kennzahl-Kacheln ---- */
  /* auto-fit statt fester Fuenferreihe: der Entwurf laesst die Spaltenzahl aus der
     Breite folgen, damit die Kacheln nie schmaler als 174 px werden. */
  .kacheln {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(174px, 1fr));
    gap: 10px;
  }
  .kachel {
    display: flex; flex-direction: column; gap: 8px;
    padding: 13px 15px; border-radius: 14px;
    border: 1px solid var(--card-border); background: var(--card-bg);
  }
  .kachel-kopf {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.8px;
    color: var(--text-secondary);
  }
  .kachel-zahl { display: flex; align-items: baseline; gap: 6px; }
  .kachel-zahl span {
    font-family: var(--font-display); font-weight: 700; font-size: 25px;
    color: #eaf2fa; line-height: 1;
  }
  .kachel-zahl i {
    font-style: normal; font-family: var(--font-mono); font-size: 10px;
    color: #7fe4ff;
  }
  .kachel-sub {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.2px;
    color: var(--text-secondary); opacity: 0.8; line-height: 1.5;
  }
  .kachel-sub:empty { display: none; }
  .kachel-balken {
    height: 4px; border-radius: 3px; overflow: hidden;
    background: rgba(255, 255, 255, 0.09);
  }
  .kachel-balken i {
    display: block; height: 100%; width: 0; border-radius: 3px;
    background: var(--color-cyan); transition: width 0.4s ease;
  }
  .kachel-spark { width: 100%; height: 26px; }
  /* Unter 1200 px zwei Reihen statt einer gequetschten. */
  @media (max-width: 1200px) {
    .kacheln { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }

  /* ---- Kopfzeile der Unterhaltung ---- */
  .chat-head { display: flex; align-items: baseline; gap: 14px; }
  .chat-head-meta {
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px;
    color: var(--text-secondary); opacity: 0.8; white-space: nowrap;
  }
  .chat-head-fill { flex: 1; }
  .chat-head-kv {
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px;
    color: var(--text-secondary); white-space: nowrap; flex-shrink: 0;
  }
  .chat-export {
    display: flex; align-items: center; gap: 7px; flex-shrink: 0;
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px;
    padding: 6px 10px; border-radius: 9px; cursor: pointer;
    border: 1px solid var(--card-border); background: transparent;
    color: var(--text-secondary);
  }
  .chat-export:hover { border-color: var(--card-border-hover); color: var(--text-primary); }

  /* ---- Nachrichtenkopf ---- */
  .chat-msg.ai .chat-role { display: flex; align-items: center; gap: 10px; }
  /* Die kleine Kugel traegt denselben Zustand wie der grosse Orb in der Live-Ansicht
     -- gleiche Farben, damit man beide Ansichten nicht getrennt lernen muss. */
  .rolle-punkt {
    width: 17px; height: 17px; border-radius: 50%; flex: 0 0 auto;
    background: radial-gradient(circle at 40% 35%, #a7ecff, #0d3050);
    box-shadow: 0 0 10px rgba(79, 216, 245, 0.45);
    transition: background 0.45s ease, box-shadow 0.45s ease;
  }
  .rolle-punkt.is-recherche {
    background: radial-gradient(circle at 40% 35%, #ffe1a8, #4a3208);
    box-shadow: 0 0 12px rgba(245, 197, 107, 0.55);
  }
  .rolle-punkt.is-denken {
    background: radial-gradient(circle at 40% 35%, #d8ccff, #241a52);
    box-shadow: 0 0 12px rgba(111, 123, 255, 0.55);
  }
  .rolle-punkt.is-antwort {
    background: radial-gradient(circle at 40% 35%, #b6f5ff, #0d3050);
    box-shadow: 0 0 14px rgba(79, 216, 245, 0.7);
  }
  /* Solange gearbeitet wird, atmet sie; steht die Antwort, ist sie ruhig. */
  .rolle-punkt.is-aktiv { animation: rolle-atmen 2.2s ease-in-out infinite; }
  @keyframes rolle-atmen {
    0%, 100% { transform: scale(1); opacity: 0.85; }
    50%      { transform: scale(1.12); opacity: 1; }
  }
  /* Beschriftung neben dem Namen, solange der Turn laeuft. Danach steht dort
     wieder die Uhrzeit samt Dauer. */
  .rolle-zustand {
    display: flex; align-items: center; gap: 6px;
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 2.4px;
    text-transform: uppercase;
  }
  .rolle-zustand:empty { display: none; }
  .rolle-zustand::before {
    content: ''; width: 4px; height: 4px; border-radius: 50%;
    background: currentColor; box-shadow: 0 0 6px currentColor;
  }
  .rolle-zustand.is-recherche { color: var(--amber, #f5c56b); }
  .rolle-zustand.is-denken    { color: #a9b4ff; }
  .rolle-zustand.is-antwort   { color: #7fe4ff; }
  .rolle-name {
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 2.4px;
    color: #7c92ad;
  }
  .rolle-zeit {
    font-family: var(--font-mono); font-size: 9.5px; color: var(--text-secondary);
    opacity: 0.75;
  }
  .rolle-zeit:empty { display: none; }

  /* ---- Fusszeile unter dem Eingabefeld ---- */
  .chat-fuss {
    display: flex; align-items: center; gap: 16px;
    padding: 9px 16px 14px;
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 1.4px;
    color: #47566a;
  }
  .chat-fuss span:last-child { white-space: nowrap; }

  /* ---- Seitenleiste ---- */
  .chat-new-btn {
    display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  .chat-suche {
    display: flex; align-items: center; gap: 8px; padding: 0 10px;
    border-radius: 10px; border: 1px solid var(--card-border);
    background: rgba(6, 10, 18, 0.45);
  }
  .chat-suche-icon { display: flex; color: var(--text-secondary); flex: 0 0 auto; }
  .chat-suche input {
    flex: 1; min-width: 0; padding: 8px 0; border: 0; background: none;
    font-family: var(--font-body); font-size: calc(12.5px * var(--chat-scale));
    color: var(--text-primary); outline: none;
  }
  .chat-suche input::-webkit-search-cancel-button { filter: grayscale(1) opacity(0.5); }

  /* Tagesueberschrift: sie ordnet die Liste, ohne selbst anklickbar zu sein. */
  .chat-gruppe {
    font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 2px;
    color: var(--text-secondary); opacity: 0.7;
    padding: 10px 4px 4px;
  }
  .chat-gruppe:first-child { padding-top: 2px; }
  .chat-item {
    display: grid; grid-template-columns: 1fr auto; align-items: baseline;
    gap: 2px 8px; padding: 9px 10px; border-radius: 10px;
    border: 1px solid transparent; cursor: pointer;
  }
  .chat-item-title {
    grid-column: 1; font-family: var(--font-body); font-weight: 500;
    font-size: calc(12.5px * var(--chat-scale)); color: var(--text-primary);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chat-item.active .chat-item-title { font-weight: 600; }
  .chat-item-meta {
    grid-column: 1; font-family: var(--font-mono);
    font-size: calc(9.5px * var(--chat-scale)); color: var(--text-secondary);
  }
  .chat-item-del { grid-column: 2; grid-row: 1 / span 2; align-self: center; }

  /* ---- Werkzeugschritte: ein Block, eine Zeile je Schritt ---- */
  .chat-tools.is-da {
    display: flex; flex-direction: column; gap: 7px;
    padding: 11px 13px; border-radius: 12px;
    background: rgba(6, 10, 18, 0.45); border: 1px solid rgba(163, 198, 255, 0.08);
  }
  .tool-zeile {
    display: grid; grid-template-columns: 16px 1fr auto; align-items: center; gap: 10px;
  }
  .tool-sym { display: flex; color: var(--color-green); }
  .tool-sym.is-aus { color: var(--text-secondary); opacity: 0.7; }
  .tool-text {
    font-family: var(--font-mono); font-size: calc(10.5px * var(--chat-scale));
    color: #b0c1d4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .tool-dauer {
    font-family: var(--font-mono); font-size: calc(9.5px * var(--chat-scale));
    color: var(--text-secondary); opacity: 0.75; white-space: nowrap;
  }

  /* ---- Gedankengang ---- */
  .chat-think-head {
    display: flex; align-items: center; gap: 9px; width: fit-content;
    padding: 6px 11px; border-radius: 9px; cursor: pointer;
    border: 1px solid rgba(111, 123, 255, 0.24); background: rgba(111, 123, 255, 0.08);
    color: #a9b4ff; font-family: var(--font-mono);
    font-size: calc(9.5px * var(--chat-scale)); letter-spacing: 1.4px;
  }
  .chat-think-head svg:last-child { opacity: 0.7; transition: transform 0.2s ease; }
  .chat-think-head.open svg:last-child { transform: rotate(180deg); }

  /* ---- Quellen als Pillen ---- */
  .src-pille {
    display: flex; align-items: center; gap: 7px; flex: 0 0 auto;
    padding: 4px 10px 4px 5px; border-radius: 999px; text-decoration: none;
    border: 1px solid rgba(163, 198, 255, 0.13); background: rgba(6, 10, 18, 0.45);
    font-family: var(--font-mono); font-size: calc(9.5px * var(--chat-scale));
    color: #8b9db2;
  }
  .src-pille:hover { border-color: var(--card-border-hover); color: var(--text-primary); }
  .src-nr {
    width: 15px; height: 15px; border-radius: 50%; flex: 0 0 auto;
    background: rgba(79, 216, 245, 0.14); color: #7fe4ff;
    display: grid; place-items: center; font-size: 8.5px;
  }

  /* Aktionen tragen jetzt Text neben dem Symbol. */
  .chat-action {
    display: inline-flex; align-items: center; gap: 7px;
    font-family: var(--font-mono); font-size: calc(9.5px * var(--chat-scale));
    letter-spacing: 1.4px; padding: 6px 10px; border-radius: 8px;
    border: 1px solid transparent; background: transparent;
  }

  /* ---- Anhang-Dialog: die zwei Ziele ---- */
  .upload-ziele {
    display: flex; align-items: center; gap: 9px; flex-wrap: wrap;
    padding: 11px 2px 2px;
  }
  .upload-ziele-text {
    font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary);
    margin-right: auto;
  }
  .ziel-btn {
    display: flex; align-items: center; gap: 8px;
    border: 1px solid rgba(163, 198, 255, 0.16); background: rgba(146, 178, 224, 0.06);
    color: #c3d2e2; border-radius: 9px; padding: 9px 14px; cursor: pointer;
    font-family: var(--font-display); font-weight: 600; font-size: 11px;
    letter-spacing: 1.8px; text-transform: uppercase;
  }
  .ziel-btn i { display: flex; }
  .ziel-btn:hover { border-color: var(--card-border-hover); background: rgba(255, 255, 255, 0.06); }
  .ziel-btn.is-primary {
    border-color: rgba(79, 216, 245, 0.38); color: #d9f3ff;
    background: linear-gradient(180deg, rgba(79, 216, 245, 0.20), rgba(79, 216, 245, 0.08));
  }
  .ziel-btn:disabled { opacity: 0.45; cursor: default; }

  /* ---- Collection ---- */
  .coll-list { display: flex; flex-direction: column; gap: 3px; max-height: 236px; overflow-y: auto; }
  /* Feste Spalten aus dem Entwurf: Symbol, Name, Chunks, Groesse, Zeitpunkt, Papierkorb. */
  .coll-row {
    display: grid; grid-template-columns: 18px minmax(0, 1fr) 96px 78px 92px auto;
    align-items: center; gap: 12px; padding: 8px 10px; border-radius: 10px;
    border: 1px solid rgba(163, 198, 255, 0.07); background: rgba(6, 10, 18, 0.4);
    font-family: var(--font-mono); font-size: 9.5px; color: #5f7085;
  }
  .coll-row:hover { border-color: rgba(163, 198, 255, 0.14); }
  .coll-sym { display: flex; color: #5f7085; }
  .coll-name {
    font-size: 10.5px; color: #dfe9f3;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .coll-num { opacity: 0.8; white-space: nowrap; }
  .coll-del {
    display: flex; align-items: center; justify-content: center;
    width: 22px; height: 22px; border: 0; padding: 0; border-radius: 6px;
    background: none; color: var(--text-secondary); cursor: pointer;
  }
  .coll-del:hover { color: var(--color-magenta); background: rgba(255, 110, 140, 0.10); }
  .coll-zahl {
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.2px;
    color: var(--text-secondary); opacity: 0.8;
  }
  .coll-btn {
    border: 1px solid var(--card-border); background: rgba(255, 255, 255, 0.03);
    color: var(--text-secondary); border-radius: 7px; padding: 3px 9px; cursor: pointer;
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.2px;
  }
  .coll-btn:hover { border-color: var(--card-border-hover); color: var(--text-primary); }
  .coll-btn:disabled { opacity: 0.5; cursor: default; }

  .coll-clear {
    border: 1px solid rgba(255, 110, 140, 0.30); background: rgba(255, 110, 140, 0.06);
    color: var(--color-magenta); border-radius: 7px; padding: 3px 9px; cursor: pointer;
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.2px;
  }
  .coll-clear:hover { background: rgba(255, 110, 140, 0.14); border-color: var(--color-magenta); }

  /* Ein Rahmen um alles: Knoepfe, Eingabe und Senden sitzen im selben Kasten,
     nicht als drei getrennte Elemente nebeneinander. */
  .chat-composer {
    display: flex; gap: 9px; align-items: flex-end;
    margin: 12px 16px 0;
    padding: 9px 11px; border-radius: 16px;
    border: 1px solid rgba(163, 198, 255, 0.13);
    background: rgba(6, 10, 18, 0.5);
  }
  .chat-attachments { padding: 0 16px; }
  #chat-input {
    flex: 1; resize: none; max-height: 180px; min-height: 34px;
    padding: 7px 2px;
    border: none; background: transparent;
    color: #e9f0f7; font-size: calc(14.5px * var(--chat-scale));
    font-family: var(--font-body); line-height: 1.5;
    outline: none;
  }
  .chat-send-btn {
    width: 38px; height: 34px; border-radius: 10px; cursor: pointer; flex-shrink: 0;
    display: grid; place-items: center;
    border: 1px solid rgba(79, 216, 245, 0.38);
    background: linear-gradient(180deg, rgba(79, 216, 245, 0.20), rgba(79, 216, 245, 0.08));
    color: #d9f3ff;
    transition: border-color 0.2s ease;
  }
  .chat-send-btn:hover { border-color: #4fd8f5; }
  .chat-send-btn:disabled { opacity: 0.4; cursor: default; }
  /* Abbrechen-Zustand: derselbe Knopf, unuebersehbar andere Farbe. */
  .chat-send-btn.is-stop, .live-btn.is-stop {
    border-color: var(--color-magenta);
    background: rgba(255, 43, 214, 0.16);
    color: var(--color-magenta);
    box-shadow: 0 0 10px rgba(255, 43, 214, 0.35);
  }
  .chat-send-btn.is-stop:hover, .live-btn.is-stop:hover {
    background: var(--color-magenta); color: #100a1e;
  }
  .chat-empty {
    margin: auto; text-align: center; color: var(--text-secondary);
    font-family: var(--font-mono);
  }

  .chat-think-body {
    display: none; font-family: var(--font-mono);
    font-size: calc(10.5px * var(--chat-scale));
    color: var(--text-secondary); padding: 11px 13px; margin: 6px 0;
    border-radius: 11px;
    border: 1px solid rgba(111, 123, 255, 0.16);
    background: rgba(111, 123, 255, 0.04);
    white-space: pre-wrap; max-height: 260px; overflow-y: auto;
  }
  .chat-think-body.open { display: block; }

  .chat-confirm {
    border: 1px solid rgba(0, 229, 255, 0.35);
    background: rgba(0, 229, 255, 0.06);
    border-radius: 10px;
    padding: calc(11px * var(--chat-scale)) calc(14px * var(--chat-scale));
    margin-bottom: 8px;
  }
  /* Loeschende Aktionen sehen anders aus als normales Schreiben -- der
     Unterschied ist die Sicherheitsidee und darf nicht im Text untergehen. */
  .chat-confirm.is-destructive {
    border-color: rgba(255, 43, 214, 0.5);
    background: rgba(255, 43, 214, 0.07);
  }
  .chat-confirm.is-pending { animation: kvCompactPulse 2.4s ease-in-out infinite; }
  .chat-confirm.is-ok { border-color: rgba(0, 255, 163, 0.4); background: rgba(0, 255, 163, 0.05); }
  .chat-confirm.is-off { opacity: 0.65; }
  .chat-confirm-head {
    font-family: var(--font-display);
    font-size: calc(12px * var(--chat-scale));
    letter-spacing: 0.5px; color: var(--text-primary); margin-bottom: 4px;
  }
  .chat-confirm-sub {
    font-family: var(--font-mono);
    font-size: calc(11.5px * var(--chat-scale));
    color: var(--text-secondary); margin-bottom: 8px;
  }
  .chat-confirm-toggle {
    font-family: var(--font-mono);
    font-size: calc(11.5px * var(--chat-scale));
    color: var(--color-blue); cursor: pointer; user-select: none; margin-bottom: 6px;
  }
  .chat-confirm-toggle:hover { text-shadow: 0 0 6px var(--color-blue); }
  .chat-confirm-body {
    display: none; margin: 0 0 8px;
    font-family: var(--font-mono);
    font-size: calc(11.5px * var(--chat-scale));
    color: var(--text-secondary); white-space: pre-wrap; word-break: break-word;
    max-height: 260px; overflow-y: auto;
    background: rgba(0, 0, 0, 0.3); border-radius: 6px; padding: 8px 10px;
  }
  .chat-confirm-body.open { display: block; }
  .chat-confirm-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .chat-confirm-btn {
    font-family: var(--font-mono);
    font-size: calc(12px * var(--chat-scale));
    padding: 6px 14px; border-radius: 6px; cursor: pointer;
    border: 1px solid #2a3650; background: #10192c; color: #cfe0ff;
    transition: all 0.2s ease;
  }
  .chat-confirm-btn:disabled { opacity: 0.45; cursor: default; }
  .chat-confirm-btn.is-ok { border-color: var(--color-green); color: var(--color-green); }
  .chat-confirm-btn.is-ok:hover:not(:disabled) { background: var(--color-green); color: #001810; }
  .chat-confirm-btn.is-danger { border-color: var(--color-magenta); color: var(--color-magenta); }
  .chat-confirm-btn.is-danger:hover:not(:disabled) { background: var(--color-magenta); color: #100a1e; }
  .chat-confirm-btn.is-plain:hover:not(:disabled) { background: #1c2b4c; }
  .chat-confirm-state {
    font-family: var(--font-mono);
    font-size: calc(11.5px * var(--chat-scale));
    color: var(--text-secondary);
  }

  .chat-attachments {
    display: flex; gap: 8px; flex-wrap: wrap;
    padding: 10px 20px 0 20px;
  }
  .chat-attach { position: relative; display: inline-flex; }
  .chat-attach img {
    height: 58px; width: 58px; object-fit: cover; border-radius: 8px;
    border: 1px solid rgba(0, 229, 255, 0.3);
  }
  .chat-attach-del {
    position: absolute; top: -6px; right: -6px; width: 18px; height: 18px;
    border-radius: 50%; background: var(--color-magenta); color: #100a1e;
    font-size: 13px; line-height: 18px; text-align: center; cursor: pointer;
    font-weight: 700;
  }
  .chat-attach-btn {
    width: 34px; height: 34px; border-radius: 10px; cursor: pointer; flex-shrink: 0;
    display: grid; place-items: center;
    border: 1px solid rgba(163, 198, 255, 0.13); background: transparent;
    color: #8b9db2;
    transition: border-color 0.2s ease, color 0.2s ease;
  }
  .chat-attach-btn:hover { border-color: rgba(79, 216, 245, 0.5); color: #fff; }
  .chat-attach-btn[hidden] { display: none; }
  .chat-images { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
  .chat-images img {
    max-height: 190px; max-width: 100%; border-radius: 10px;
    border: 1px solid rgba(0, 229, 255, 0.25);
  }

  @media (max-width: 900px) {
    .chat-shell { grid-template-columns: 1fr; }
    .chat-sidebar { display: none; }
  }

  /* ===== WebGL-Hintergrund ===== */
  /* Liegt hinter allem und laeuft in jeder Ansicht. Faellt WebGL aus, bleibt der
     Canvas leer und die bg-glow-Kreise darunter tragen die Optik allein. */
  #bg-canvas {
    position: fixed;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: -2;
    pointer-events: none;
    opacity: 0.85;
  }

  /* ===== Anmeldung ===== */
  .auth-gate {
    position: fixed;
    inset: 0;
    z-index: 10000;
    display: flex;
    align-items: center;
    justify-content: center;
    background: radial-gradient(ellipse at 50% 40%, rgba(20, 16, 46, 0.72), var(--bg-color) 68%);
    animation: auth-in 0.5s ease;
  }
  .auth-gate.is-leaving { animation: auth-out 0.45s ease forwards; }
  @keyframes auth-in  { from { opacity: 0; } to { opacity: 1; } }
  @keyframes auth-out { to { opacity: 0; visibility: hidden; } }

  #auth-orb-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    opacity: 0.75;
  }

  .auth-card {
    position: relative;
    z-index: 2;
    width: min(372px, calc(100vw - 40px));
    padding: 30px 30px 26px;
    border-radius: 20px;
    border: 1px solid rgba(163, 198, 255, 0.13);
    background: linear-gradient(180deg, rgba(146, 178, 224, 0.10), rgba(146, 178, 224, 0.03));
    backdrop-filter: blur(26px) saturate(115%);
    -webkit-backdrop-filter: blur(26px) saturate(115%);
    box-shadow: 0 1px 0 rgba(255, 255, 255, 0.07) inset, 0 30px 70px rgba(0, 0, 0, 0.55);
  }
  .auth-card::after {
    content: ''; position: absolute; bottom: 0; right: 0; width: 8px; height: 8px;
    border-bottom: 2px solid var(--color-magenta); border-right: 2px solid var(--color-magenta);
    border-bottom-right-radius: 16px;
  }
  .auth-brand {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: 19px;
    letter-spacing: 5px;
    text-align: center;
    background: linear-gradient(120deg, #fff 18%, var(--color-cyan) 42%, var(--color-magenta) 60%, #fff 82%);
    background-size: 200% auto;
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: shine 5s linear infinite;
  }
  .auth-sub {
    font-family: var(--font-mono);
    font-size: 9px;
    letter-spacing: 2.4px;
    text-transform: uppercase;
    color: var(--text-secondary);
    text-align: center;
    margin: 6px 0 26px;
  }
  .auth-form { display: flex; flex-direction: column; gap: 6px; }
  .auth-label {
    font-family: var(--font-mono);
    font-size: 8.5px;
    letter-spacing: 1.8px;
    text-transform: uppercase;
    color: var(--text-secondary);
    margin-top: 8px;
  }
  .auth-input {
    padding: 11px 13px;
    border-radius: 9px;
    border: 1px solid #243150;
    background: rgba(9, 12, 24, 0.9);
    color: var(--text-primary);
    font-family: var(--font-body);
    font-size: 16px;
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  .auth-input:focus {
    border-color: var(--color-blue);
    box-shadow: 0 0 0 3px rgba(0, 229, 255, 0.12);
  }
  .auth-row { display: flex; flex-direction: column; gap: 6px; }
  .auth-remember {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-top: 16px;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12.5px;
    color: var(--text-secondary);
    user-select: none;
  }
  .auth-remember input {
    width: 16px;
    height: 16px;
    accent-color: var(--color-blue);
    cursor: pointer;
  }
  .auth-remember:hover { color: var(--text-primary); }
  .auth-remember-hint { opacity: 0.6; }
  .auth-btn {
    margin-top: 20px;
    padding: 12px;
    border-radius: 9px;
    cursor: pointer;
    border: 1px solid var(--color-blue);
    background: rgba(0, 229, 255, 0.12);
    color: var(--color-blue);
    font-family: var(--font-mono);
    font-size: 13px;
    letter-spacing: 2px;
    text-transform: uppercase;
    transition: background 0.25s ease, color 0.25s ease, box-shadow 0.25s ease;
  }
  .auth-btn:hover:not(:disabled) {
    background: var(--color-blue);
    color: #001018;
    box-shadow: 0 0 18px rgba(0, 229, 255, 0.45);
  }
  .auth-btn:disabled { opacity: 0.5; cursor: default; }
  .auth-msg {
    min-height: 20px;
    margin-top: 12px;
    font-family: var(--font-mono);
    font-size: 12px;
    text-align: center;
    color: var(--color-magenta);
  }
  .auth-msg.is-ok { color: var(--color-green); }
  .auth-switch {
    margin-top: 4px;
    padding: 8px;
    border: none;
    background: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: 12.5px;
    text-decoration: underline;
    text-underline-offset: 3px;
    transition: color 0.2s ease;
  }
  .auth-switch:hover { color: var(--color-blue); }
  .auth-switch[hidden] { display: none; }

  /* ===== Konto-Menue in der Navigation ===== */
  /* nowrap und flex-shrink: 0 sind Pflicht -- sonst bricht die Kopfzeile um,
     sobald die Kompaktierungszeile dazukommt. */
  /* Eigene Pille wie im Entwurf. nowrap und flex-shrink: 0 sind Pflicht -- sonst
     bricht die Kopfzeile um, sobald die Kompaktierungszeile dazukommt. */
  .nav-kv {
    display: flex; align-items: center; gap: 12px; flex-shrink: 0; white-space: nowrap;
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.6px;
    color: var(--text-secondary);
    padding: 6px 12px; border-radius: 999px;
    border: 1px solid rgba(163, 198, 255, 0.10);
    background: rgba(10, 15, 25, 0.5);
  }
  .nav-kv-label { color: #5f7085; }
  .nav-kv-pct {
    font-family: var(--font-display); font-weight: 700; font-size: 13px;
    letter-spacing: 0; color: #eaf2fa; white-space: nowrap;
  }
  .nav-kv-bar {
    flex: 0 0 58px; height: 3px; border-radius: 999px;
    background: rgba(79, 216, 245, 0.14); overflow: hidden;
  }
  .nav-kv-bar i {
    display: block; height: 100%; width: 0;
    background: linear-gradient(90deg, #4fd8f5, #35e3a4);
    transition: width 0.4s ease, background 0.4s ease;
  }
  .nav-kv.is-hoch .nav-kv-bar i { background: linear-gradient(90deg, #f5c56b, #ff6e8c); }
  .nav-kv-comp { color: #5f7085; letter-spacing: 1.4px; }
  .nav-kv-comp:empty { display: none; }

  .nav-account { position: relative; }
  .nav-account-btn {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 5px 12px 5px 7px;
    border-radius: 999px;
    cursor: pointer;
    border: 1px solid rgba(163, 198, 255, 0.13);
    background: rgba(10, 15, 25, 0.55);
    color: #c3d2e2;
    font-family: var(--font-mono); font-size: 10px;
    transition: border-color 0.25s ease, background 0.25s ease, color 0.25s ease;
  }
  .nav-account-btn:hover { border-color: rgba(79, 216, 245, 0.5); color: #fff; }
  /* Initialen statt Punkt: bei mehreren Konten erkennt man das eigene schneller. */
  .account-dot {
    display: grid; place-items: center; flex-shrink: 0;
    width: 22px; height: 22px; border-radius: 50%;
    background: linear-gradient(135deg, #4fd8f5, #6f7bff);
    color: #06121c;
    font-family: var(--font-display); font-weight: 700; font-size: 10px;
  }
  /* Administratoren tragen eine andere Farbe: die Rolle entscheidet, was auf dem
     Rechner passieren darf -- man soll sie sehen, ohne das Menue zu oeffnen. */
  .account-dot.is-admin { background: linear-gradient(135deg, #35e3a4, #4fd8f5); }
  .account-ident {
    display: flex; flex-direction: column; align-items: flex-start; gap: 1px;
    line-height: 1.2; text-align: left;
  }
  .account-ident-rolle {
    font-size: 8.5px; letter-spacing: 1.4px;
    text-transform: uppercase; color: #5f7085;
  }
  /* Abmelden liegt neben dem Konto, nicht darin: ein Klick statt Menue aufklappen. */
  .nav-logout {
    display: grid; place-items: center; flex-shrink: 0;
    width: 32px; height: 32px;
    border-radius: 10px; cursor: pointer;
    border: 1px solid rgba(163, 198, 255, 0.13);
    background: rgba(10, 15, 25, 0.55);
    color: #8b9db2;
    transition: border-color 0.25s ease, color 0.25s ease;
  }
  .nav-logout:hover { border-color: rgba(255, 110, 140, 0.4); color: #ff98b0; }
  .account-menu {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    min-width: 210px;
    z-index: 60;
    padding: 6px;
    border-radius: 10px;
    border: 1px solid var(--card-border);
    background: rgba(12, 14, 28, 0.97);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    box-shadow: 0 14px 40px rgba(0, 0, 0, 0.6);
  }
  .account-menu-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 9px 10px;
    border: none;
    border-radius: 7px;
    background: none;
    cursor: pointer;
    color: var(--text-primary);
    font-family: var(--font-body);
    font-size: 14.5px;
    transition: background 0.18s ease, color 0.18s ease;
  }
  .account-menu-item:hover { background: rgba(0, 229, 255, 0.1); color: #fff; }
  .account-menu-item.is-danger:hover { background: rgba(255, 43, 214, 0.13); color: var(--color-magenta); }

  /* ===== Dialog (Dateien) ===== */
  .modal {
    position: fixed;
    inset: 0;
    z-index: 9000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .modal[hidden] { display: none; }
  .modal-backdrop {
    position: absolute;
    inset: 0;
    background: rgba(2, 3, 8, 0.72);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }
  .modal-card {
    position: relative;
    z-index: 2;
    width: min(760px, 100%);
    max-height: min(660px, 88vh);
    display: flex;
    flex-direction: column;
    border-radius: 18px;
    border: 1px solid rgba(163, 198, 255, 0.14);
    background: linear-gradient(180deg, rgba(146, 178, 224, 0.11), rgba(146, 178, 224, 0.035));
    backdrop-filter: blur(26px) saturate(115%);
    -webkit-backdrop-filter: blur(26px) saturate(115%);
    box-shadow: 0 22px 64px rgba(0, 0, 0, 0.65);
    animation: modal-in 0.22s ease;
  }
  @keyframes modal-in {
    from { opacity: 0; transform: translateY(10px) scale(0.99); }
    to   { opacity: 1; transform: none; }
  }
  .modal-head {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 15px 18px;
    border-bottom: 1px solid rgba(180, 130, 255, 0.16);
    font-family: var(--font-display);
    font-size: 14px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: var(--text-primary);
  }
  .modal-path {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 11.5px;
    text-transform: none;
    letter-spacing: 0;
    color: var(--text-secondary);
  }
  .modal-close {
    border: none;
    background: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-size: 24px;
    line-height: 1;
    padding: 0 2px;
  }
  .modal-close:hover { color: var(--color-magenta); }
  .modal-body { padding: 18px; overflow-y: auto; }
  body.modal-open { overflow: hidden; }

  /* ===== Dateien ===== */
  .dropzone {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 9px;
    padding: 26px 20px;
    margin-bottom: 14px;
    border-radius: 14px;
    border: 1px dashed rgba(79, 216, 245, 0.3);
    background: rgba(79, 216, 245, 0.05);
    cursor: pointer;
    transition: border-color 0.25s ease, background 0.25s ease, transform 0.25s ease;
  }
  .dropzone:hover, .dropzone:focus-visible {
    border-color: var(--color-blue);
    background: rgba(0, 229, 255, 0.07);
    outline: none;
  }
  .dropzone.is-over {
    border-color: var(--color-magenta);
    background: rgba(255, 43, 214, 0.09);
    transform: scale(1.008);
  }
  .dropzone-icon {
    font-size: 30px;
    color: var(--color-blue);
    text-shadow: 0 0 14px rgba(0, 229, 255, 0.55);
  }
  .dropzone-title {
    font-family: var(--font-display);
    font-size: 15px;
    letter-spacing: 1.2px;
    color: var(--text-primary);
  }
  .dropzone-hint {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    text-align: center;
  }

  .upload-queue { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
  .upload-queue:empty { display: none; }
  /* Laufender Upload: Name links, Zustand rechts, darunter der Balken ueber die
     ganze Breite. Die fertige Warteliste ist .upload-zeile und hat ihr eigenes Raster. */
  .upload-item {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px 12px;
    padding: 10px 12px;
    border-radius: 12px;
    border: 1px solid rgba(163, 198, 255, 0.09);
    background: rgba(6, 10, 18, 0.45);
    font-family: var(--font-mono);
    font-size: 10.5px;
  }
  .upload-item-name { color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .upload-item-state { color: var(--text-secondary); white-space: nowrap; }
  .upload-item.is-ok .upload-item-state { color: var(--color-green); }
  .upload-item.is-error .upload-item-state { color: var(--color-magenta); }
  .upload-item .progress-container { grid-column: 1 / -1; }

  .files-toolbar {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .files-list { display: flex; flex-direction: column; gap: 6px; }
  .file-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 12px;
    align-items: center;
    padding: 10px 13px;
    border-radius: 9px;
    border: 1px solid rgba(180, 130, 255, 0.12);
    background: rgba(18, 20, 40, 0.34);
    transition: border-color 0.2s ease, background 0.2s ease;
  }
  .file-row:hover { border-color: rgba(0, 229, 255, 0.3); background: rgba(0, 229, 255, 0.04); }
  .file-name {
    font-family: var(--font-mono);
    font-size: 13.5px;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .file-name .file-kind {
    color: var(--color-cyan);
    margin-right: 8px;
  }
  .file-meta {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .file-actions { display: flex; gap: 6px; }
  .file-btn {
    padding: 5px 11px;
    border-radius: 6px;
    cursor: pointer;
    border: 1px solid var(--card-border);
    background: rgba(0, 0, 0, 0.25);
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: 11.5px;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    transition: all 0.2s ease;
  }
  .file-btn:hover:not(:disabled) { border-color: var(--color-blue); color: var(--color-blue); }
  .file-btn.is-danger:hover:not(:disabled) { border-color: var(--color-magenta); color: var(--color-magenta); }
  .file-btn:disabled { opacity: 0.4; cursor: default; }

  .files-log {
    margin-top: 14px;
    border-radius: 9px;
    border: 1px solid rgba(0, 229, 255, 0.22);
    background: rgba(0, 0, 0, 0.32);
    overflow: hidden;
  }
  .files-log-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text-secondary);
    border-bottom: 1px solid rgba(0, 229, 255, 0.14);
  }
  .files-log-close { cursor: pointer; font-size: 16px; color: var(--color-magenta); }
  .files-log pre {
    margin: 0;
    padding: 12px;
    max-height: 300px;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-primary);
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ===== Benutzerkonten ===== */
  .users-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
  .user-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 10px;
    align-items: center;
    padding: 8px 11px;
    border-radius: 8px;
    border: 1px solid rgba(180, 130, 255, 0.12);
    background: rgba(18, 20, 40, 0.34);
  }
  .user-name { font-family: var(--font-mono); font-size: 13px; color: var(--text-primary); }
  .user-badge {
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 5px;
    border: 1px solid var(--color-purple);
    color: var(--color-purple);
    cursor: pointer;
    background: none;
    transition: all 0.2s ease;
  }
  .user-badge.is-admin { border-color: var(--color-green); color: var(--color-green); }
  .user-badge:hover { box-shadow: 0 0 8px currentColor; }
  .user-del { cursor: pointer; color: var(--text-secondary); font-size: 16px; padding: 0 4px; }
  .user-del:hover { color: var(--color-magenta); }
  .user-row.is-self .user-del { visibility: hidden; }
  .users-add { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .users-input {
    flex: 1 1 130px;
    min-width: 0;
    padding: 8px 11px;
    border-radius: 7px;
    border: 1px solid #243150;
    background: rgba(9, 12, 24, 0.9);
    color: var(--text-primary);
    font-family: var(--font-body);
    font-size: 14px;
    outline: none;
  }
  .users-input:focus { border-color: var(--color-blue); }
  .users-role { flex: 0 0 auto; }

  /* Rollengebundene Navigation: 'chat'-Konten sehen den Monitor gar nicht erst.
     Verbindlich ist das Gate an der Route, nicht diese Zeile. */
  body:not(.role-admin) .nav-link[data-role="admin"] { display: none; }

  /* Die Grundgroessen oben stehen bereits auf rund 85% -- hier bleiben nur die
     Abstaende, die im Monitor enger sein duerfen als anderswo. */
  #view-monitor .panel-header { padding: 11px 15px; }
  #view-monitor .stats-list { gap: 7px; }
  #view-monitor .metric-block { padding: 13px; }

  /* ===== Unterreiter im Monitor =====
     Jeder Reiter fuellt eine Bildschirmhoehe; was nicht hineinpasst, hat einen
     eigenen Reiter statt unter dem Falz zu verschwinden. */
  /* Eigener Kasten, nur so breit wie die Reiter -- nicht ueber die ganze Zeile. */
  .subtabs {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    width: fit-content;
    padding: 6px;
    border-radius: 13px;
    border: 1px solid var(--card-border);
    background: rgba(10, 12, 26, 0.45);
  }
  .subtab {
    padding: 7px 14px;
    border: 1px solid transparent;
    border-radius: 9px;
    background: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 1.8px;
    text-transform: uppercase;
    transition: color 0.2s ease, background 0.2s ease, border-color 0.2s ease;
  }
  .subtab:hover { color: var(--text-primary); background: rgba(0, 229, 255, 0.06); }
  .subtab.active {
    color: #fff;
    border-color: var(--color-blue);
    background: rgba(0, 229, 255, 0.1);
    box-shadow: inset 0 0 12px rgba(0, 229, 255, 0.06);
  }

  .subview { display: none; flex-direction: column; gap: 10px; }
  .subview.active { display: flex; }

  /* Der Ablaufverfolgungs-Reiter gehoert dem iframe -- es nimmt die Resthoehe. */
  #tab-traces { flex: 1; min-height: 0; }
  #tab-traces .traces-panel-live { flex: 1; min-height: 0; }

  /* ===== Benutzerkonten: Punkt, Name, Rolle, Aktion ===== */
  .user-row {
    grid-template-columns: 26px minmax(0, 1fr) 168px auto;
    gap: 14px; padding: 10px 12px; border-radius: 11px;
    border-color: rgba(163, 198, 255, 0.09); background: rgba(6, 10, 18, 0.4);
  }
  .user-avatar {
    width: 26px; height: 26px; border-radius: 50%;
    background: linear-gradient(135deg, #35e3a4, #4fd8f5);
  }
  .user-avatar.is-admin {
    background: linear-gradient(135deg, #4fd8f5, #6f7bff);
  }
  .user-name-line { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .user-name { font-family: var(--font-body); font-weight: 600; font-size: 13px; }
  .user-last {
    font-family: var(--font-mono); font-size: 9.5px;
    color: var(--text-secondary); opacity: 0.75;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  /* Im Entwurf ist die Rolle nur Text. Bei uns schaltet ein Klick sie um --
     deshalb Rahmen erst beim Zeigen, statt dauerhaft als Pille. */
  .user-badge {
    font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px;
    text-transform: none; color: var(--text-secondary);
    background: none; border: 1px solid transparent; border-radius: 8px;
    padding: 5px 8px; text-align: left;
  }
  .user-badge.is-admin { border-color: transparent; color: var(--color-cyan); }
  .user-badge:hover { border-color: var(--card-border-hover); box-shadow: none; }
  .user-del {
    display: flex; align-items: center; gap: 7px;
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.4px;
    padding: 6px 10px; border-radius: 8px; border: 1px solid transparent;
    background: none; color: var(--text-secondary);
  }
  .user-del:hover { color: var(--color-magenta); border-color: rgba(255, 110, 140, 0.32); }
  .user-eigen {
    font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.4px;
    padding: 6px 10px; color: var(--text-secondary); opacity: 0.6;
  }

  @media (max-width: 700px) {
    .argus-nav { flex-wrap: wrap; }
    .nav-account { margin-left: auto; }
    .file-row { grid-template-columns: 1fr auto; }
    .file-actions { grid-column: 1 / -1; justify-content: flex-end; }
  }

'''

# Glas-Thema aus Claude Design. Laedt NACH argus.css und ueberschreibt nur
# Tokens und Oberflaechen, keine Struktur -- Rueckbau = <link> entfernen.
ARGUS_GLASS_CSS = r'''/* argus-glass.css -- Glas-Thema fuer ARGUS.
   NACH argus.css laden; die Datei ueberschreibt nur Tokens und Oberflaechen,
   keine Struktur. Rueckbau = <link> entfernen.
   Einbau in Setup_Dashboard.py bei den uebrigen <link rel="stylesheet">:
     <link rel="stylesheet" href="/static/argus-glass.css"> */

:root {
  /* kuehle Neutrals statt Lila-Dominanz */
  --bg-color: #070a12;
  --card-bg: rgba(146, 178, 224, 0.055);
  --card-border: rgba(163, 198, 255, 0.11);
  --card-border-hover: rgba(79, 216, 245, 0.45);
  --color-blue: #4fd8f5;
  --color-cyan: #4fd8f5;
  --color-magenta: #ff6e8c;   /* Warn/Destruktiv jetzt Rose, nicht Neon-Magenta */
  --color-purple: #6f7bff;
  --color-green: #35e3a4;
  --text-primary: #e9f0f7;
  --text-secondary: #7e91a8;

  /* eigene Tokens dieses Themas */
  --glass: linear-gradient(180deg, rgba(146,178,224,.075), rgba(146,178,224,.028));
  --glass-strong: linear-gradient(180deg, rgba(146,178,224,.10), rgba(146,178,224,.035));
  --glass-line: 0 1px 0 rgba(255,255,255,.05) inset;
  --glass-shadow: 0 16px 36px rgba(0,0,0,.35);
  --text-dim: #5f7085;
  --text-faint: #4d5c70;
  --amber: #f5c56b;
}

/* Grundflaeche: ein ruhiger Verlauf, kein Raster ueber die ganze Seite */
body {
  background-color: var(--bg-color);
  background-image: radial-gradient(ellipse 90% 70% at 50% -10%, #101a2b 0%, #070a12 55%, #05070d 100%);
  background-size: auto;
  font-weight: 400;
}
/* Scanlines und die drei farbigen Glows raus -- die Glasflaechen tragen die Tiefe */
body::after { display: none; }
.bg-glow { display: none; }

/* ---- Glasflaechen ---- */
.argus-nav,
.panel,
.chat-sidebar,
.chat-main,
.metric-block {
  background: var(--glass);
  border: 1px solid var(--card-border);
  border-radius: 16px;
  backdrop-filter: blur(20px) saturate(110%);
  -webkit-backdrop-filter: blur(20px) saturate(110%);
  box-shadow: var(--glass-line), var(--glass-shadow);
}
/* der magenta Farbbalken links an der Kopfzeile entfaellt */
.argus-nav::before { display: none; }
.panel::before, .panel::after { display: none; }
.panel:hover { border-color: var(--card-border); box-shadow: var(--glass-line), var(--glass-shadow); }

/* ---- Navigation ---- */
.nav-link { color: var(--text-secondary); border-radius: 10px; letter-spacing: 2.2px; }
.nav-link:hover { color: var(--text-primary); background: rgba(79,216,245,.07); }
.nav-link.active {
  color: #d6f4ff;
  border: 1px solid rgba(79,216,245,.40);
  background: linear-gradient(180deg, rgba(79,216,245,.20), rgba(79,216,245,.07));
  box-shadow: 0 6px 18px rgba(15,120,150,.18);
}
.nav-account-btn { border-radius: 999px; background: rgba(10,15,25,.55); border: 1px solid rgba(163,198,255,.13); }
.nav-account-btn:hover { border-color: rgba(79,216,245,.5); background: rgba(79,216,245,.08); }

/* Logo: Conic-Regenbogen und Flicker weg, ruhiger Ring */
.logo-icon { color: #7fe4ff; filter: drop-shadow(0 0 7px rgba(79,216,245,.35)); animation: none; }
.logo-text h2 { animation: none; background: none; -webkit-text-fill-color: #eaf2fa; filter: none; letter-spacing: 3px; }
.auth-brand { animation: none; background: none; -webkit-text-fill-color: #eaf2fa; filter: none; }

/* ---- Monitor: dichter, kleinere Schrift ---- */
.panel { padding: 14px 16px; }
.panel-header h2 { font-size: 11px; letter-spacing: 2.2px; color: #c3d2e2; }
/* Groesse und Sperrung der Panel-Kopfzeile stehen im Grund-CSS auf Entwurfsstand;
   das erste Feld ist der Name, alle weiteren sind Beiwerk. Hier nur die Farbe. */
.panel-header-desc { color: #c3d2e2; }
.panel-header-desc > span:not(:first-child) { color: var(--text-faint); }
.metric-block { padding: 13px 15px; border-radius: 14px; }
.metric-block:hover { border-color: var(--card-border); }
.metric-block-title { font-size: 8.5px; letter-spacing: 1.8px; color: var(--text-dim); }
.metric-label { font-size: 9px; letter-spacing: 1.4px; color: var(--text-dim); }
.metric-value { font-size: 12.5px; }
.kv-compact-line { font-size: 9px; color: var(--text-faint); }

/* ---- Chat ---- */
.chat-item.active { background: rgba(79,216,245,.08); border-color: rgba(79,216,245,.28); }
.chat-item:hover { background: rgba(146,178,224,.05); }
.chat-msg.user .chat-bubble {
  border: 1px solid rgba(79,216,245,.24);
  background: linear-gradient(180deg, rgba(79,216,245,.11), rgba(79,216,245,.05));
  border-radius: 14px 14px 4px 14px;
  color: #e4f4fd;
}
.chat-msg.ai .chat-bubble { background: none; border: none; color: #dfe9f3; }
.chat-bubble code { color: #a9b4ff; }
.chat-think-head { color: #a9b4ff; border: 1px solid rgba(111,123,255,.24); background: rgba(111,123,255,.08); border-radius: 9px; }
.chat-think-head:hover { background: rgba(111,123,255,.14); color: #d3d9ff; }
.tool-text { color: #b0c1d4; }
.tool-zeile:hover .tool-text { color: var(--text-primary); }
/* Radien und Farben der Eingabezeile stehen im Grund-CSS auf Entwurfsstand --
   hier bleibt nur der etwas kraeftigere Hover des Senden-Knopfs. */
.chat-send-btn:hover { background: linear-gradient(180deg, rgba(79,216,245,.28), rgba(79,216,245,.12)); }
.chat-action:last-child:hover { color: #ff98b0; background: rgba(255,110,140,.10); }

/* ---- Traces: Phoenix darf die Flaeche haben ---- */
.traces-panel-live { min-height: 640px; }
.traces-panel-live #traces-frame { min-height: 560px; border: 1px solid var(--card-border); background: #05080f; border-radius: 12px; }

/* Der Entwurf kennt kein seitenfuellendes Canvas. Die Tiefe kommt aus dem
   Seitenverlauf und zwei Auflagen je Abschnitt -- der Nebel aus bg.js faerbte
   alles lila und liegt deshalb still. In der Live-Ansicht bringt der Orb Sterne
   und Warp ohnehin selbst mit. */
#bg-canvas { display: none; }
body.live-aktiv .live-stage { background: #04060c; border-color: rgba(163,198,255,.09); }

/* Radialer Kern und feines Raster, beides nach innen ausgeblendet. Liegt hinter
   dem Inhalt, deshalb pointer-events aus. */
.live-stage::before,
.live-stage::after {
  content: ''; position: absolute; inset: 0; pointer-events: none; z-index: 0;
}
.live-stage::before {
  background: radial-gradient(ellipse 62% 55% at 50% 48%, rgba(16,38,66,.5), rgba(4,6,12,0) 72%);
}
.live-stage::after {
  opacity: .45;
  background-image:
    linear-gradient(rgba(70,140,210,.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(70,140,210,.05) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(circle at 50% 50%, #000 12%, transparent 70%);
  -webkit-mask-image: radial-gradient(circle at 50% 50%, #000 12%, transparent 70%);
}

/* Live-Ansicht: der Orb liegt auf schwarzem Grund, kein Kartenrand drumherum */
#view-live .panel { box-shadow: var(--glass-line), var(--glass-shadow); }

::placeholder { color: #55647a; }
'''

JS_COMMON = r'''const $ = (id) => document.getElementById(id);

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
    throw new Error("Nicht angemeldet");
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
function tcell(ts) { return (ts || "").slice(0, 16).replace("T", " "); }

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
'''

JS_AUTH = r'''// ===== Anmeldung, Rollen, Kontoverwaltung =====
// Die Sitzung liegt in einem HttpOnly-Cookie: JavaScript kommt nicht daran, ein
// eingeschleustes Skript kann sie also nicht auslesen. Der Zustand hier ist nur
// eine Kopie fuer die Anzeige -- entschieden wird serverseitig an jeder Route.
window.argusSession = { authenticated: false, username: null, role: null, userId: null };

(function () {
  let mode = 'login';            // 'login' | 'setup' (erstes Konto) | 'register'
  let booted = false;
  let allowRegister = false;

  function el(id) { return document.getElementById(id); }

  function setMessage(text, ok) {
    const box = el('auth-msg');
    if (!box) return;
    box.textContent = text || '';
    box.classList.toggle('is-ok', !!ok);
  }

  // Der Punkt sagt nur eine Sache: hat das Backend geantwortet. Ueber SGLang,
  // Qdrant und TTS ist vor der Anmeldung nichts bekannt -- die Zeile daneben
  // zaehlt deshalb auf, was laeuft, ohne einen Zustand zu behaupten.
  function dienst(erreichbar) {
    const box = el('auth-dienst');
    if (!box) return;
    box.className = 'auth-dienst ' + (erreichbar ? 'ist-da' : 'ist-weg');
    box.innerHTML = '<i></i>' + (erreichbar ? 'DIENST ERREICHBAR' : 'DIENST NICHT ERREICHBAR');
  }

  function symbole() {
    if (!window.argusIcon) return;
    const logo = el('auth-logo');
    if (logo) logo.innerHTML = window.argusIcon('ring', 46);
    document.querySelectorAll('#auth-gate [data-icon]').forEach(function (n) {
      n.innerHTML = window.argusIcon(n.dataset.icon, 15);
    });
  }

  function applyMode(next) {
    mode = next;
    const isSetup = next === 'setup';
    const isRegister = next === 'register';
    const neuesKonto = isSetup || isRegister;

    el('auth-sub').textContent = isSetup ? 'ERSTES KONTO \u00b7 WIRD ADMINISTRATOR'
      : (isRegister ? 'NEUES KONTO \u00b7 CHAT & SPRACHE'
                    : 'LOKALER AGENT \u00b7 ZUGANG ERFORDERLICH');
    el('auth-submit-text').textContent = neuesKonto ? 'Konto anlegen' : 'Anmelden';
    el('auth-confirm-row').style.display = neuesKonto ? '' : 'none';
    // Beim Anlegen des ersten Kontos keine Dauer-Anmeldung anbieten -- das ist ein
    // einmaliger Vorgang, danach folgt ohnehin die normale Anmeldung.
    el('auth-remember-row').style.display = isSetup ? 'none' : '';
    el('auth-pass').setAttribute('autocomplete', neuesKonto ? 'new-password' : 'current-password');

    const umschalter = el('auth-switch');
    umschalter.hidden = isSetup || !allowRegister;
    umschalter.textContent = isRegister
      ? 'Zurück zur Anmeldung'
      : 'Noch kein Konto? Hier registrieren';

    setMessage(isSetup ? 'Noch kein Konto vorhanden. Dieses erste Konto wird Administrator.'
      : (isRegister ? 'Das Konto darf chatten und sprechen.' : ''), neuesKonto);
  }

  // Zwei Zeichen fuer den Kreis in der Kopfzeile. Trennzeichen einer Mailadresse
  // oder eines Namens zaehlen als Wortgrenze; sonst die ersten beiden Buchstaben.
  function initialen(name) {
    const teile = String(name || '').split(/[.\-_@\s]+/).filter(Boolean);
    if (teile.length >= 2) return (teile[0][0] + teile[1][0]).toUpperCase();
    return String(name || '?').slice(0, 2).toUpperCase();
  }

  function applySession(data) {
    window.argusSession = {
      authenticated: true,
      username: data.username,
      role: data.role || 'chat',
      userId: data.user_id
    };
    const admin = data.role === 'admin';
    document.body.classList.toggle('role-admin', admin);
    el('account-name').textContent = data.username;
    el('account-role').textContent = admin ? 'Administrator' : 'Chat & Sprache';
    el('account-dot').textContent = initialen(data.username);
    el('account-dot').classList.toggle('is-admin', admin);
    // Rollengebundene Bedienelemente ausserhalb der Navigation (Datei-Knopf im Chat).
    document.querySelectorAll('[data-role="admin"]').forEach(function (node) {
      if (node.classList.contains('nav-link')) return;
      node.hidden = !admin;
    });
  }

  function openApp() {
    const gate = el('auth-gate');
    const shell = el('app-shell');
    shell.hidden = false;
    gate.classList.add('is-leaving');
    setTimeout(function () {
      gate.style.display = 'none';
      if (window.argusAuthOrb) window.argusAuthOrb.stop();
    }, 450);
    if (!booted) { booted = true; if (window.argusBoot) window.argusBoot(); }
    else if (window.argusRouteNow) window.argusRouteNow();
    if (window.argusOps) window.argusOps.heartbeat();
  }

  function showGate(reason) {
    const gate = el('auth-gate');
    el('app-shell').hidden = true;
    gate.style.display = '';
    gate.classList.remove('is-leaving');
    window.argusSession = { authenticated: false, username: null, role: null, userId: null };
    document.body.classList.remove('role-admin');
    if (window.argusAuthOrb) window.argusAuthOrb.start();
    if (window.argusOps) window.argusOps.halt();
    if (window.vcOrb) window.vcOrb.stop();
    el('auth-pass').value = '';
    if (reason) setMessage(reason);
  }

  async function submit(ev) {
    ev.preventDefault();
    const btn = el('auth-submit');
    const username = el('auth-user').value.trim();
    const password = el('auth-pass').value;
    if (!username || !password) { setMessage('Benutzername und Passwort ausfüllen.'); return; }
    if (mode === 'setup' || mode === 'register') {
      if (username.length < 3) { setMessage('Benutzername mindestens 3 Zeichen.'); return; }
      if (password.length < 8) { setMessage('Passwort mindestens 8 Zeichen.'); return; }
      if (password !== el('auth-pass2').value) { setMessage('Die Passwörter stimmen nicht überein.'); return; }
    }
    btn.disabled = true;
    setMessage('');
    try {
      const PFADE = {
        setup: '/v1/dashboard/auth/setup',
        register: '/v1/dashboard/auth/register',
        login: '/v1/dashboard/auth/login'
      };
      // Die Rolle wird NIE mitgeschickt -- sie steht serverseitig fest (setup: admin,
      // register: chat). Ein Feld hier wäre nur eine Einladung, es zu verbiegen.
      const body = { username: username, password: password };
      if (mode !== 'setup') body.remember = el('auth-remember').checked;
      const res = await apiPost(PFADE[mode], body);
      applySession(res);
      el('auth-pass').value = '';
      if (el('auth-pass2')) el('auth-pass2').value = '';
      openApp();
    } catch (e) {
      setMessage((e && e.message) || 'Anmeldung fehlgeschlagen.');
    } finally {
      btn.disabled = false;
    }
  }

  async function logout() {
    try { await apiPost('/v1/dashboard/auth/logout'); } catch (e) { /* Cookie ist so oder so weg */ }
    showGate('Abgemeldet.');
  }

  async function changePassword() {
    const next = prompt('Neues Passwort (mindestens 8 Zeichen):');
    if (next === null) return;
    if (next.length < 8) { alert('Passwort mindestens 8 Zeichen.'); return; }
    try {
      await apiPost('/v1/dashboard/auth/password', { password: next });
      alert('Passwort geändert.');
    } catch (e) {
      alert('Fehlgeschlagen: ' + ((e && e.message) || e));
    }
  }

  function bindAccountMenu() {
    const btn = el('account-btn'), menu = el('account-menu');
    if (!btn || !menu) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function () {
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    });
    menu.addEventListener('click', function (ev) { ev.stopPropagation(); });
    el('account-logout').addEventListener('click', logout);
    el('account-password').addEventListener('click', changePassword);
  }

  async function init() {
    bindAccountMenu();
    el('auth-form').addEventListener('submit', submit);
    el('auth-switch').addEventListener('click', function () {
      applyMode(mode === 'register' ? 'login' : 'register');
      el('auth-user').focus();
    });
    if (window.argusAuthOrb) window.argusAuthOrb.start();
    symbole();
    try {
      const state = await api('/v1/dashboard/auth/state');
      dienst(true);
      if (state.authenticated) { applySession(state); openApp(); return; }
      allowRegister = !!state.allow_register;
      applyMode(state.needs_setup ? 'setup' : 'login');
    } catch (e) {
      // Backend nicht erreichbar: Anmeldemaske stehen lassen, nichts vortäuschen.
      dienst(false);
      applyMode('login');
      setMessage('Backend nicht erreichbar.');
    }
    el('auth-user').focus();
  }

  window.argusAuth = {
    // Von api() gerufen, sobald irgendein Aufruf 401 liefert.
    sessionLost: function () {
      if (!window.argusSession.authenticated) return;
      showGate('Sitzung abgelaufen — bitte erneut anmelden.');
    },
    logout: logout,
    isAdmin: function () { return window.argusSession.role === 'admin'; }
  };

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();

// ===== Kontoverwaltung (Monitor-Ansicht, nur Administratoren) =====
(function () {
  let bound = false;

  function msg(text, ok) {
    const box = $('users-msg');
    if (!box) return;
    box.textContent = text || '';
    box.style.color = ok ? 'var(--color-green)' : 'var(--color-magenta)';
  }

  async function render() {
    const box = $('users-list');
    if (!box) return;
    try {
      const res = await api('/v1/dashboard/users');
      const users = res.users || [];
      box.innerHTML = users.map(function (u) {
        const self = u.is_self;
        const admin = u.role === 'admin';
        const zuletzt = u.last_active
          ? 'zuletzt ' + tcell(u.last_active)
          : 'noch nicht benutzt';
        // Der Farbverlauf des Punktes trennt Administratoren von Chatkonten auf
        // einen Blick -- dieselbe Aussage wie die Rollenbeschriftung daneben.
        return "<div class='user-row" + (self ? " is-self" : "") + "'>" +
          "<span class='user-avatar" + (admin ? " is-admin" : "") + "'></span>" +
          "<span class='user-name-line'>" +
            "<span class='user-name'>" + escapeHtml(u.username) +
              (self ? " <span class='muted' style=\"font-size:11px;\">(du)</span>" : "") + "</span>" +
            "<span class='user-last'>" + zuletzt + " · " + u.chats
              + (u.chats === 1 ? " Chat · " : " Chats · ") + u.messages
              + (u.messages === 1 ? " Nachricht" : " Nachrichten") + "</span>" +
          "</span>" +
          "<button class='user-badge" + (admin ? " is-admin" : "") + "' data-role-id='" + u.id +
            "' data-role='" + (admin ? "chat" : "admin") + "' title='Rolle umschalten'>" +
            (admin ? "ADMINISTRATOR" : "NUR CHAT &amp; SPRACHE") + "</button>" +
          (self
            ? "<span class='user-eigen'>EIGENES KONTO</span>"
            : "<button class='user-del' type='button' data-del-id='" + u.id + "' data-name='"
              + escapeHtml(u.username) + "' title='Konto löschen'>"
              + (window.argusIcon ? window.argusIcon('trash', 12) : '') + "ENTFERNEN</button>") +
          "</div>";
      }).join('') || '<div class="muted">Keine Konten.</div>';

      box.querySelectorAll('[data-role-id]').forEach(function (b) {
        b.addEventListener('click', function () { setRole(b.dataset.roleId, b.dataset.role); });
      });
      box.querySelectorAll('[data-del-id]').forEach(function (b) {
        b.addEventListener('click', function () { remove(b.dataset.delId, b.dataset.name); });
      });
    } catch (e) {
      box.innerHTML = '<div class="muted" style="color:var(--color-magenta);">Konten nicht ladbar.</div>';
    }
  }

  async function setRole(id, role) {
    try {
      await apiPost('/v1/dashboard/users/' + id + '/role?role=' + encodeURIComponent(role));
      msg('Rolle geändert.', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  async function remove(id, name) {
    // Mit dem Konto gehen seine Chatverläufe -- das steht in der Rückfrage, weil es
    // sich nicht rückgängig machen lässt.
    if (!confirm('Konto "' + name + '" mitsamt allen Chatverläufen löschen?')) return;
    try {
      const res = await api('/v1/dashboard/users/' + id, { method: 'DELETE' });
      msg('Konto gelöscht (' + res.chats_removed + ' Chats entfernt).', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  async function create(ev) {
    ev.preventDefault();
    const name = $('user-new-name').value.trim();
    const pass = $('user-new-pass').value;
    const role = $('user-new-role').value;
    if (name.length < 3 || pass.length < 8) {
      msg('Benutzername ab 3, Passwort ab 8 Zeichen.');
      return;
    }
    try {
      await apiPost('/v1/dashboard/users', { username: name, password: pass, role: role });
      $('user-new-name').value = '';
      $('user-new-pass').value = '';
      msg('Konto "' + name + '" angelegt.', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  window.argusUsers = {
    refresh: function () {
      if (!bound) {
        const form = $('users-add');
        if (form) { form.addEventListener('submit', create); bound = true; }
      }
      render();
    }
  };
})();
'''

JS_BG = r'''// ===== WebGL-Hintergrund: driftender Nebel + Sternfeld =====
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
'''

# Der Orb selbst: ES-Modul aus Claude Design, woertlich uebernommen bis auf
# pause()/resume(). Wird als klassisches Skript geladen und ueber einen
# Modul-Schnipsel in index.html an window.argusCreateOrb gehaengt.
JS_ORB_MODULE = r'''// Argus-Orb v3
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
'''

JS_ORB = r'''window.currentLlmThroughput = 0;
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
'''

JS_VOICE = r'''// Abbruch des laufenden Turns. Das Backend merkt den Abbruch ueber
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
        // Entschieden wird in der Chat-Ansicht oder per Telegram; hier nur der
        // Hinweis, damit die Wartezeit erklaert ist.
        onConfirmation: function (ev) {
          showAi(ev.phase === 'pending'
            ? '🔐 Freigabe nötig — im Chat oder per Telegram bestätigen …'
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

'''

JS_OPS = r'''function getStatusLabel(status) {
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
// die Kennzahlen sind Administratoren vorbehalten, ein 'chat'-Konto braucht aber
// dieselbe Aussage darueber, ob das Backend lebt.
async function probeConnection() {
  try {
    await api("/v1/dashboard/config");
    noteConnection(true);
    //--- Die Kopfzeile lebt in JEDER Ansicht, der Monitor-Poll laeuft nur im
    //    Monitor. Ein Administrator holt sich die Zahl deshalb hier mit -- ein
    //    'chat'-Konto darf /summary nicht und ueberspringt es.
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
  const d = new Date(iso);
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
function ensureTracesLoaded() {
  const f = $("traces-frame");
  if (f && !f.src && f.dataset.src) f.src = f.dataset.src;
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
    if (!f) return;
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
'''

JS_FILES = r'''// ===== Dateien-Ansicht: hochladen, entpacken, indexieren =====
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
'''

JS_CHAT = r'''// ===== Chat-Ansicht: Verlauf aus der Datenbank, Streaming, Werkzeug-Schritte =====
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

// Der Anhang-Knopf erscheint nur, wenn SGLANG_ENABLE_MULTIMODAL aktiv ist. Ein Knopf,
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
  const d = new Date(iso);
  if (isNaN(d)) return 'ÄLTER';
  const heute = new Date(); heute.setHours(0, 0, 0, 0);
  const tag = new Date(d); tag.setHours(0, 0, 0, 0);
  const diff = Math.round((heute - tag) / 86400000);
  return diff <= 0 ? 'HEUTE' : diff === 1 ? 'GESTERN' : 'ÄLTER';
}

function chatUhrzeit(iso) {
  const d = new Date(iso || '');
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
  const d = new Date(iso || '');
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
// (Cooldown). Nach ID abgelegt, damit das resolved-Ereignis seine Karte findet --
// auch wenn per Telegram entschieden wurde.
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
    }
    else if (sch.art === 'denken') chatAddReasoning(msg, sch.text || '');
  });
  chatAddSources(msg, steps.quellen || []);
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
    onSources: function (items) { chatAddSources(ai, items); chatScroll(); },
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
  const notiz = 'Ich habe folgende Datei(en) nach C:\\Argus_Workspace\\Uploads gelegt: '
    + namen.join(', ') + '. ';
  const rest = (inp.value || '').trim();
  inp.value = notiz + rest;
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
  inp.dispatchEvent(new Event('input'));
}
'''

JS_ROUTER = r'''// ===== Hash-Router: vier Ansichten, EIN Dokument =====
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
  // Ein 'chat'-Konto, das #/monitor eintippt, landet auf der Live-Ansicht. Die Daten
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
'''


def _asset_version(*sources: str) -> str:
    """Kurzer Inhalts-Hash ueber alle Frontend-Quellen.

    Ohne ihn liefert der Browser nach einem Dashboard-Update die ALTEN Skripte aus dem
    Cache -- die Dateinamen bleiben ja gleich, und der Rebuild sieht wirkungslos aus.
    Ein Hash ueber ALLE Dateien statt je Datei: die Skripte haengen voneinander ab,
    gemischte Staende waeren schlimmer als ein Neuladen zu viel."""
    import hashlib
    h = hashlib.sha256()
    for s in sources:
        h.update(s.encode("utf-8"))
    return h.hexdigest()[:10]


#--- Reihenfolge ist bindend: common.js definiert die Helfer, die alle uebrigen
#--- Dateien benutzen; auth.js braucht bg.js (Orb der Anmeldemaske) und ruft am Ende
#--- den Router. Diese Liste speist zugleich den Cache-Hash und das Schreiben.
JS_FILES_TO_WRITE = [
    ("common.js", "JS_COMMON"),
    ("bg.js", "JS_BG"),
    ("orb.js", "JS_ORB"),
    ("auth.js", "JS_AUTH"),
    ("voice.js", "JS_VOICE"),
    ("ops.js", "JS_OPS"),
    ("files.js", "JS_FILES"),
    ("chat.js", "JS_CHAT"),
    ("argus-orb.js", "JS_ORB_MODULE"),
    ("router.js", "JS_ROUTER"),
]


def copy_fonts(static_dir: Path) -> None:
    """Kopiert die Schriften aus assets/fonts/ nach static/fonts/.

    Binaerdateien kann kein Generator erzeugen -- sie liegen deshalb versioniert im
    Projekt (wie voice_ref/) und werden hier nur heruebergereicht. Fehlen sie, ist
    das kein Abbruchgrund: die Fallback-Stacks in :root tragen die Oberflaeche mit
    System-Schriften weiter."""
    quelle = BASE_DIR / "assets" / "fonts"
    ziel = static_dir / "fonts"
    if not quelle.is_dir():
        _log(f"WARNUNG: {quelle} fehlt -- die Oberflaeche laeuft mit System-Schriften.")
        return
    dateien = sorted(quelle.glob("*.woff2"))
    if not dateien:
        _log(f"WARNUNG: keine .woff2 in {quelle} -- die Oberflaeche laeuft mit System-Schriften.")
        return
    ziel.mkdir(parents=True, exist_ok=True)
    kopiert = 0
    for f in dateien:
        z = ziel / f.name
        #--- Nur bei Aenderung schreiben, wie writefile: sonst wechselt bei jedem Lauf
        #--- die Datei-Zeit und Docker verwirft die Image-Schicht ohne Grund.
        if z.exists() and z.stat().st_size == f.stat().st_size:
            continue
        z.write_bytes(f.read_bytes())
        kopiert += 1
    _log(f"Schriften: {len(dateien)} Dateien in {ziel} ({kopiert} aktualisiert).")


def main() -> None:
    static = BASE_DIR / "rag_backend" / "static"
    js = static / "js"
    sources = {name: globals()[var] for name, var in JS_FILES_TO_WRITE}
    copy_fonts(static)

    version = _asset_version(ARGUS_CSS, ARGUS_GLASS_CSS, *sources.values())
    index_html = re.sub(
        r'(src="js/[a-z-]+\.js|href="argus(?:-glass)?\.css)"',
        lambda m: f'{m.group(1)}?v={version}"',
        INDEX_HTML,
    )

    #--- Die Skript-Namen in index.html und diese Liste muessen sich decken: eine
    #--- Datei, die hier fehlt, waere im Browser ein 404 -- und der Rest der
    #--- Oberflaeche liefe in einen ReferenceError.
    referenced = set(re.findall(r'src="js/([a-z-]+\.js)', INDEX_HTML))
    written = set(sources)
    if referenced != written:
        _log(f"WARNUNG: index.html verweist auf {sorted(referenced - written)}, "
             f"nicht geschrieben: {sorted(written - referenced)}")

    writefile(static / "index.html", index_html, "dashboard index.html", do_dedent=False)
    writefile(static / "argus.css", ARGUS_CSS, "dashboard argus.css", do_dedent=False)
    writefile(static / "argus-glass.css", ARGUS_GLASS_CSS, "dashboard argus-glass.css",
              do_dedent=False)
    for name, content in sources.items():
        writefile(js / name, content, f"dashboard js/{name}", do_dedent=False)
    _log(f"Setup_Dashboard abgeschlossen (Asset-Version {version}).")
    #--- Nur beim Solo-Lauf ausgeben: laeuft das Skript als letzter Schritt von Setup1,
    #--- steht der Hinweis mitten in dessen Ausgabe und widerspricht der Kette
    #--- (nach Setup1 kommt Setup2, nicht der Build).
    if __name__ == "__main__":
        #--- Die Seiten werden per COPY ins rag-backend-Image gebacken, ein blosses
        #--- 'up -d' zeigt also weiter die alte Version. Hier genuegt der gezielte
        #--- Rebuild -- es haben sich nur die statischen Dateien geaendert.
        _log("Naechster Schritt: docker compose up -d --build rag-backend")


if __name__ == "__main__":
    main()
