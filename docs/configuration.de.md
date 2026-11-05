# Konfigurationsreferenz

*Deutsch · [English](configuration.md) · [Zurück zur README](../README.de.md)*

Wo die Schalter liegen und wie man sie anwendet, steht in der
[README](../README.de.md#konfiguration). Diese Seite behandelt die einzelnen Bereiche.

## Schlüssel und Geheimnisse — `API_Tokens/`

Der Ordner ist per `.gitignore` ausgeschlossen und auf dem Host auf den eigenen Benutzer
beschränkt. Alles, was man dort im Klartext einträgt, ersetzt `Setup1.py` beim nächsten
Lauf durch einen verschlüsselten Wert (`ENC:…`, Fernet mit `fernet_key.txt`). Die Container
bekommen die Dateien als `/run/secrets` gemountet und entschlüsseln im Prozess.

| Datei | Wer legt sie an | Pflicht? | Zweck |
|---|---|---|---|
| `HF_TOKEN.txt` | du | ja | Modelldownloads von HuggingFace |
| `Gemini.txt` | du | nein | Cloud-Missionen und `cloud_ask`; leer = Cloud-Stufe aus |
| `telegram.txt` | du | nein | Bot-Token und Chat-ID, zwei Zeilen `KEY=VALUE` |
| `LANGSMITH_API_KEY.txt` | du | nein | Tracing in die LangSmith-Cloud statt lokal in Phoenix |
| `ssh_user.txt`, `ssh_key`, `ssh_key.pub`, `ssh_password.txt` | `setup_ssh.ps1` | — | Zugang der Action-Engine zum Host; das Passwort wird nach der Umstellung auf Schlüssel geleert |
| `fernet_key.txt` | `Setup1.py` | — | Schlüssel für alles Verschlüsselte: Secrets, Chatverläufe |
| `jwt_secret.txt`, `webui_secret_key.txt`, `service_token.txt` | `Setup1.py` | — | Anmeldung, Open-WebUI-Kopplung |
| `postgres_password.txt`, `sglang_api_key.txt`, `searxng_secret_key.txt`, `audit_hmac_key.txt` | `Setup1.py` | — | interne Dienste, Signatur des Audit-Protokolls |

Der `fernet_key.txt` ist der Generalschlüssel. Geht er verloren, sind Verläufe und
verschlüsselte Token nicht mehr lesbar; `Setup1.py` bricht deshalb bei einem ungültigen Key
hart ab, statt still einen neuen zu erzeugen.

## Telegram einrichten

1. In Telegram `@BotFather` öffnen, `/newbot`, Namen vergeben. Der Token sieht so aus:
   `8123456789:AAF…`.
2. Den neuen Bot anschreiben — irgendeine Nachricht, damit ein Chat existiert.
3. Die eigene Chat-ID holen: im Browser
   `https://api.telegram.org/bot<TOKEN>/getUpdates` aufrufen, im JSON steht
   `"chat":{"id":123456789,…}`.
4. `API_Tokens/telegram.txt` befüllen:

   ```
   TELEGRAM_BOT_TOKEN=8123456789:AAF…
   TELEGRAM_CHAT_ID=123456789
   ```

5. `python Setup3.py` (prüft die Werte und verschlüsselt sie), dann
   `docker compose up -d rag-backend`.

Der Bot bedient **genau diese eine Chat-ID**, alle anderen werden ignoriert. Er versteht
Text, Sprachnachrichten (werden transkribiert, Antwort wahlweise als Sprache — `/voice`
schaltet um) und Fotos. Über denselben Kanal kommen die Bestätigungskarten für schreibende
Aktionen und die Ergebnisse von Cloud-Missionen.

Ohne Telegram funktioniert die Freigabe im Argus Chat über eine Karte im Verlauf. Nur der
Weg über Open WebUI kann keine Karte zeigen — dort bleibt Telegram für schreibende Aktionen
Pflicht.

## Cloud-Stufe (Gemini)

Einen Key in [Google AI Studio](https://aistudio.google.com/) erzeugen, in
`API_Tokens/Gemini.txt` eintragen, `python Setup4.py`, `docker compose up -d rag-backend`.

Was dann geht: `cloud_ask` (eine einzelne Frage an das große Modell, etwa wenn der lokale
Agent an einer Host-Aufgabe mehrfach gescheitert ist) und **Missionen** — Planer, Arbeiter,
Prüfer, mit Websuche über die eigene SearXNG. Die Modellketten, Ratenlimits (voreingestellt
auf den kostenlosen Tarif: 8 Anfragen pro Minute, 200 pro Tag), Rundenzahl und
Ergebnisgröße stehen im `SETUP4`-Block von `Setup4.py`.

Vor jedem Aufruf nach draußen läuft die Pseudonymisierung: E-Mail, IBAN, Telefonnummern und
Geburtsdaten per Muster, freie Namen per lokalem Modell als Named-Entity-Erkennung, Ersatz
durch Platzhalter wie `[PERSON_1]`. Scheitert dieser Schritt, wird **nicht gesendet**
(`CLOUD_REDACTION_FAIL_CLOSED`). Wer die Stufe ganz aus haben will: `Gemini.txt` leer
lassen oder `CLOUD_AGENTS_ENABLED` auf `false`.

## Rechnersteuerung abschalten

Wer nur Chat, Dokumente und Recherche will: oben in `Setup3.py`
`ACTION_ENGINE_ENABLED = "false"` setzen, `python Setup3.py`, `docker compose up -d rag-backend`.
Die Host-Werkzeuge werden dann gar nicht erst registriert, das Backend öffnet keine
SSH-Verbindung, und `setup_ssh.ps1` muss nicht gelaufen sein — der Benutzer `argus` wird
nicht gebraucht.

## Vision einschalten

Das Modell ist multimodal, der Bild-Encoder kostet aber VRAM, der sonst dem Kontext gehört.
Deshalb ist der Pfad standardmäßig aus. Einschalten in `Setup2.py`, `LLM_CONFIG`:

```python
"sglang_enable_multimodal": "true",
"max_images_per_request":   2,       # je Bild rund 1600 Token
```

Dann `python Setup2.py` und `docker compose up -d sglang rag-backend`. Nach dem ersten
Start mit Vision im Log von SGLang nachsehen, was vom KV-Pool übrig ist:

```bash
docker compose logs sglang | grep max_total_num_tokens
```

Liegt der Wert deutlich unter dem angeforderten `sglang_kv_pool_tokens`, den Kontext
entsprechend nachziehen (nächster Abschnitt). Bilder kommen dann per Ziehen in den Chat
oder als Foto über Telegram; Text in Bildern gilt als Daten, nicht als Anweisung.

## Kontext vergrößern

Vier Werte in `LLM_CONFIG` (`Setup2.py`) bestimmen das Fenster:

| Wert | Bedeutung |
|---|---|
| `sglang_max_sequence_len` | Obergrenze, die SGLang angefordert bekommt (`--context-length`) |
| `sglang_kv_pool_tokens` | angeforderter KV-Pool (`--max-total-tokens`) — **Anforderung, keine Zusage** |
| `effective_context_tokens` | Deckel, den der Agent sich selbst setzt; bindend ist immer der kleinere von diesem Wert und dem real gemessenen Pool |
| `sglang_mem_fraction_static` | der OOM-Knopf: Anteil des VRAM, den SGLang belegen darf (0.90) |

SGLang nimmt beim Start den kleineren Wert aus Anforderung und dem, was in den VRAM passt,
und schreibt das Ergebnis als `max_total_num_tokens` ins Log. Der Agent fragt diesen
echten Wert zur Laufzeit ab und plant damit — deshalb kann man die drei Token-Werte
großzügig setzen, ohne dass etwas kaputtgeht. Mit mehr VRAM (24 GB und aufwärts) lohnt
es sich, alle drei auf 40 000 oder mehr zu setzen und `mem_fraction` bei 0.90 zu lassen.
Bricht SGLang mit Out-of-Memory ab, `mem_fraction` senken oder die Anforderung verkleinern.

## Modell tauschen

Der Tausch ist ein Eintrag in `LLM_CONFIG`, aber drei Dinge entscheiden, ob er
funktioniert:

```python
"model_name":            "QuantTrio/Qwen3.5-9B-AWQ",
"sglang_model_revision": "938f8e3ef86c9d1e9bec3705e149694c172592f1",
"sglang_quantization":   "",            # leer: aus der Modellkonfiguration erkannt
"sglang_dtype":          "float16",
"tool_call_parser":      "qwen3_coder",
"reasoning_parser":      "qwen3",
```

**Die Revision mitziehen.** SGLang lädt mit `--trust-remote-code`, führt also Python aus
dem HuggingFace-Repo aus. Der Pin auf einen Commit ist Lieferketten-Schutz und muss beim
Modellwechsel neu gesetzt werden:

```bash
curl -s https://huggingface.co/api/models/<repo> | python -c "import json,sys;print(json.load(sys.stdin)['sha'])"
```

**Den Tool-Call-Parser zum Modell wählen.** Das ist die Stelle, die beim Tausch am
häufigsten kaputtgeht. Der Agent bekommt Werkzeugaufrufe nur, wenn SGLang das Format des
Modells versteht: Qwen3.5 gibt seine Aufrufe als XML im `qwen3_coder`-Stil aus, ältere Qwen
und die meisten Modelle mit Hermes-Template als JSON (`hermes` bzw. `qwen`). Steht der
falsche Parser drin, meldet SGLang `Failed to parse JSON part`, und der Agent antwortet
leer oder ruft nie ein Werkzeug auf. Nicht auf `auto` verlassen — für Qwen3.5 rät es falsch.
Dasselbe für `reasoning_parser`: er muss zu den Denk-Tags des Modells passen, sonst landet
der Gedankengang in der Antwort.

**dtype und Quantisierung.** Qwen3.5 (GDN-Architektur) braucht `float16` für Modell und
Conv-Cache, sonst stürzt der erste Forward im Triton-Kernel ab; ein anderes Modell braucht
das nicht. AWQ wird aus der Modellkonfiguration erkannt, GPTQ oder FP8 trägt man in
`sglang_quantization` ein. Das Modell muss mit KV-Pool in den VRAM passen — die 13 GB des
9B-AWQ sind auf 16 GB die Obergrenze.

Danach `python Setup2.py`, `docker compose up -d sglang rag-backend`, das SGLang-Log auf
`max_total_num_tokens` prüfen und die Testsuite laufen lassen (siehe [Betrieb](../README.de.md#betrieb)).
Das Backend enthält einen kleinen Normalisierer für doppelt JSON-kodierte
Werkzeugargumente, wie Qwen sie manchmal liefert; andere Modelle haben andere Macken, die
man erst im Betrieb sieht. Vision und Sprache (`SOUL.md` setzt Deutsch und Du) sind
Eigenschaften des Modells, nicht des Systems.

## Stimme

Ohne Referenzstimme spricht Argus mit einer der eingebauten Stimmen (`QWEN_VOICE` in
`Setup1.py`: `vivian`, `serena`, `ryan`, `aiden`, `eric`, `dylan`, `uncle_fu`, `ono_anna`,
`sohee`). Für eine eigene Stimme genügt eine Aufnahme:

- `voice_ref/stimme.wav` — 10 bis 20 Sekunden, ein Sprecher, ruhig und ohne Hall; WAV genügt, die Abtastrate wird beim Laden auf 24 kHz umgerechnet
- `voice_ref/stimme.txt` — der gesprochene Text, Wort für Wort

Dann in `Setup1.py` `"QWEN_CLONE_REF_WAV": "/app/voice_ref/stimme.wav"` setzen,
`python Setup1.py`, `docker compose up -d tts-service`. Die Stimme wird beim Start einmal
geladen und für alle Antworten benutzt; fehlt die Datei, fällt der Dienst auf die
Preset-Stimme zurück statt zu sterben. `QWEN_SEED` fest zu lassen verhindert, dass die
Stimme von Satz zu Satz driftet.

Das Repository enthält **keine** Referenzstimme. Nimm deine eigene oder eine, für die du
die Rechte hast — die Stimme einer anderen Person zu klonen ist ohne ihre Einwilligung
nicht in Ordnung, und Filmton ist obendrein urheberrechtlich geschützt.

## Weitere Schalter

In `Setup1.py`: Rate-Limits je Endpunkt, Selbstregistrierung (`DASHBOARD_ALLOW_REGISTER`,
neue Konten sind immer `chat`), Lebensdauer von Anmeldungen (`JWT_EXPIRE_DAYS`,
`DASHBOARD_SESSION_HOURS`), Aufbewahrung hochgeladener Dateien (`UPLOAD_RETENTION_HOURS`),
Tracing lokal in Phoenix oder in die LangSmith-Cloud, Host-Ordner für Eval-Läufe und
Audit-Dateien (`ARGUS_EVALS_DIR`, Standard `C:\Argus_Workspace\Evals`). In `Setup2.py`: Chunking, Anzahl der
gelesenen Quellen, Zeitlimits der Sub-Agenten, Größe des Gedächtnisauszugs. In
`Setup1.py` ausserdem: Audio-Cache des Sprachdienstes (`TTS_CACHE_MAX_ENTRIES`,
`TTS_CACHE_MAX_MB`) — ein zweites Vorlesen desselben Textes kostet damit nichts. In
`Setup3.py`: Wartezeit der Freigabe (`CONFIRMATION_TIMEOUT_SECONDS`), Laufzeit einer
Aufgabenfreigabe (`TASK_APPROVAL_TTL_SECONDS`), Ausgabekappung der Host-Werkzeuge. Jeder
Wert trägt im Skript einen Kommentar, was er tut.
