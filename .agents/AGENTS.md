# Workspace Rules - ARGUS

- Quellcodedateien im Backend (z. B. `rag_backend/*.py`) werden über Vorlagen in `Setup1.py`, `Setup2.py`, `Setup3.py` und `Setup4.py` generiert; das Dashboard aus `Setup_Dashboard.py`.
- Zur schnellen Entwicklung und zum Testen dürfen die generierten Live-Dateien direkt editiert und getestet werden.
- **WICHTIG:** Am Ende müssen alle Änderungen in den Template-String der jeweiligen `SetupN.py` übertragen werden. Das Ausführen des Setup-Skripts und ein Docker-Rebuild sind nur dann nötig, wenn die Live-Dateien/Container nicht bereits während des Testens aktualisiert wurden (um redundante Rebuilds zu vermeiden). Der Zustand von SetupN.py und den Live-Dateien muss am Ende konsistent sein.
- Innerhalb der `r"""…"""`-Templates nie `"""` verwenden (beendet den String) — Docstrings dort immer mit `'''`.
- Bewusste Architekturentscheidungen (Trade-offs) respektieren und nicht ungefragt „wegoptimieren"; eigene Vorschläge sind willkommen, aber begründet.

## Kommunikationsstil
- Fachlich präzise, direkt, keine KI-Standardfloskeln („Natürlich!", „Lass mich zusammenfassen").
- Fließtext vor Listen: nummerierte Listen nur für Schritt-für-Schritt-Anweisungen, Tabellen für Vergleiche ab drei Objekten.

## Token-Budget & Inferenz-Realität
- SGLang-Inferenz läuft GPU-bound auf einer 16-GB-Blackwell-Karte (sm_120). FP8-KV-Cache läuft nicht auf Blackwell.
- Der reale KV-Pool (`max_total_num_tokens`) steht erst nach dem Boot im SGLang-Log; `SGLANG_KV_POOL_TOKENS` und `EFFECTIVE_CONTEXT_TOKENS` sind Anforderungen, das Backend fragt den echten Wert zur Laufzeit ab (`_serving_budget()`).
- Multi-Agent-Split-Parallelisierung statt sequentieller Searcher-Thinker-Judge-Ketten (spart Latenz).

## Key Workarounds & Logik-Details
- **OWUI-Bypass:** Open WebUI schickt Requests mit `### Task:`-Präfix. `main.py` filtert das RAG-Präfix heraus, damit der Titelgenerator die Antwort nicht verschluckt.
- **Double-encoded Tool-Calls:** Der Monkeypatch in `agent.py` normalisiert doppelt JSON-kodierte Tool-Argumente. Nicht entfernen.
- **Tool-Call-Parser:** Qwen3.5 emittiert `qwen3_coder`-XML, kein Hermes-JSON. `tool_call_parser` in `LLM_CONFIG` nie auf `auto` lassen.
- **TTS warm-clone:** C++-Patch an `tts-server.cpp` (qwentts.cpp, MIT) lädt die Referenzstimme einmalig beim Start (TTFA ~8.7 s → ~1.6 s). Threading-Sweet-Spot `GGML_NUM_THREADS = 6`, `OMP_NUM_THREADS = 1`, `OPENBLAS_NUM_THREADS = 1`. OpenMP im Build aus (verhindert Idle-RTF-Spike).
- **WebSearcher-Subagent:** läuft immer im Reasoning-Modell. Grounding-Hardening: Konfidenz-Regel (≥2 Quellen) plus Reformulierung bei dünnen Ergebnissen.
- **Vision:** `sglang_enable_multimodal` (Setup2). Bild-Budget 4000 Zeichen (`_budget_chars`). Bilder nur in der aktuellen Nachricht (History text-only). Text im Bild gilt als passive Daten (Injection-Schutz).
- **Cloud-Agenten & Missionen:** Provider ist Gemini (der frühere Claude-Provider wurde entfernt). Planer (lokal oder Gemini) → Worker (Gemini mit Search-Grounding) → Reviewer/Synthese (lokales Modell). OWUI-Push-JWT ist mit dem *rohen Inhalt* von `WEBUI_SECRET_KEY_FILE` signiert (nicht dem SHA-Hash, auch nicht bei „ENC:").
- **Drei-Schichten-Missionszustellung:**
  1. Stream-Follow-Stream (SSE, pollt `mission_step`, ⏳-Fortschritt live; blockiert Push via `_FOLLOWED`).
  2. /event-Push (asynchron, persistiert).
  3. Brücken-Nachlieferung in `main.py` via `undelivered_for_chat` (Haystack-Prüfung).
- **Identität:** `SOUL/SKILL/AGENTS.md` werden einmalig nach `C:\Argus_Workspace\identity\` geseedet und live gelesen; der Platzhalter `{{OWNER}}` wird beim Seed aus `OWNER_NAME` gefüllt. Die Live-Dateien haben Vorrang, `verify_identity_defaults` warnt nur bei Drift.

## Härtung & Security
- Alle Ports nur auf `127.0.0.1`; Dashboard mit Anmeldung und den Rollen `admin`/`chat`.
- `calc-sandbox` läuft isoliert mit Pandas-Support, read-only Volumes, ohne Netz.
- `classify_script` läuft fail-closed (Output-Redirection `>`/`>>` ist WRITE, unbekannte Cmdlets sind WRITE, Aufruf über Variable `& $x` und `${x}`-Zuweisungen sind DESTRUCTIVE). Native-Exec-Veto für `cmd`/`powershell`/`pwsh`/`.exe`-Pfade gilt für jede Klassifizierung.
- Der Benutzer `argus` ist Mitglied der Administrators-Gruppe (nötig für Updates/Dienste); CLM wird pro Sitzung gesetzt und ist ohne WDAC nur Defense-in-Depth — die Grenze sind Gate und Bestätigung.
- TOFU-Host-Key-Pinning statt paramiko-AutoAddPolicy; SSH-Connection-Pooling.
- Memory: dateibasiert (`MEMORY.md` und `USER.md` in `C:\Argus_Workspace\users\<id>\`). Reflexion periodisch nach WebUI-Aktivität.
