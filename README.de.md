# Argus

*Deutsch · [English](README.md)*

**Ein KI-Assistent für Unterlagen, die das Haus nicht verlassen dürfen.** Argus läuft
vollständig auf eigener Hardware und wird im Browser bedient. Das Sprachmodell hat keinen
Zugang zum Internet. Sobald ein Gespräch Firmendokumente gelesen hat, ist in diesem Gespräch
jeder Weg nach außen gesperrt.

![Frage an die eigenen Dokumente: Suchschritt, gefundene Dokumente mit Relevanz, Antwort](docs/images/chat-dokumente.png)

**Inhalt:** [Wofür](#wofür) · [Was heute geht](#was-heute-geht) · [Gemessen](#gemessen) ·
[Warum sicher](#warum-argus-sicher-ist) · [Audit und SIEM](#wer-hat-was-getan-auditprotokoll-und-siem) ·
[Websuche](#warum-websuche-und-wie-sie-abgesichert-ist) ·
[Modell](#das-modell-ist-austauschbar) · [Was fehlt](#was-bis-zum-einsatz-fehlt) ·
[Architektur](#architektur) · [Sicherheitskonzept](#sicherheitskonzept) ·
[Einrichten](#einrichten) · [Betrieb auf einem Server](#betrieb-auf-einem-server) ·
[Dienste](#dienste) · [Stellschrauben](#stellschrauben-env) ·
[Dokumentensuche](#dokumentensuche) · [Sicherheits-Schalter](#sicherheits-schalter) ·
[Tests und Messungen](#tests-und-messungen) · [Lizenz](#lizenz)

## Wofür

Viele Firmen nutzen einen Cloud-Assistenten wie Microsoft 365 Copilot für allgemeines
internes Wissen. Unterlagen mit hohem Schutzbedarf bleiben dabei außen vor, weil sie nicht in
einen Cloud-Dienst sollen oder dürfen. Bei einem Dienstleister der Automobilindustrie ist das
ein großer Teil der täglichen Arbeit:

- Kundenunterlagen unter Geheimhaltungsvereinbarung, Lastenhefte, Zeichnungen
- Daten zu Prototypen und Vorserien
- Reklamationen, 8D-Berichte, Prüf- und Arbeitsanweisungen
- Verträge, Preise, Lieferantenbewertungen

Für diese Unterlagen gibt es heute meist gar keine KI-Unterstützung. Argus schließt die
Lücke: dieselbe Art von Assistent, aber auf einem Rechner im eigenen Netz, mit einem Modell,
das technisch nichts hinausschicken kann.

**Ein Beispiel.** Eine Qualitätsingenieurin fragt: „Was war die Ursache der Reklamation
RK-2025-0041 und welche Sofortmaßnahmen wurden beschlossen?“ Argus sucht in den
aufgenommenen Berichten und antwortet nach rund 20 Sekunden. Über der Antwort steht, in
welchen Dokumenten die Textstellen gefunden wurden (Aufnahme oben). Fragt sie im selben Gespräch nach einer Web-Recherche, lehnt
Argus ab: Dieses Gespräch hat Firmendaten gelesen, also geht aus ihm nichts mehr nach außen.
Für die Recherche beginnt sie ein neues Gespräch, in dem keine Firmendaten liegen.

![Sperre nach Firmendaten: Im Gespräch mit dem Reklamationsbericht weist Argus die Websuche ab, die Schritte tragen ein Schloss](docs/images/sperre.png)

Weitere Aufgaben, für die Argus gedacht ist:

- Fragen an Richtlinien, Verträge und Anweisungen, mit Quelle statt aus dem Gedächtnis
- Berichte zusammenfassen und vergleichen („Gab es dieses Fehlerbild schon einmal?“)
- Recherche zu Normen, Vorschriften und Herstellerangaben, mit Quellen
- Einarbeitung: Wer neu ist, fragt die Unterlagen statt die Kollegen

### Argus neben einem Cloud-Assistenten

Argus ersetzt einen Cloud-Assistenten nicht, es ergänzt ihn um den Teil, der dort nicht
hingehört.

| | Cloud-Assistent (z. B. Microsoft 365 Copilot) | Argus |
|---|---|---|
| Gerechnet wird | beim Anbieter | auf eigener Hardware im eigenen Netz |
| Gedacht für | allgemeines internes Wissen, Office-Alltag | Unterlagen, die nicht in eine Cloud sollen |
| Schutz vor Datenabfluss | vertraglich und durch den Anbieter | technisch: Modell ohne Internet, Sperre nach Firmendaten, bei jeder Änderung automatisch geprüft |
| Modell | vom Anbieter vorgegeben | frei wählbar und austauschbar |
| Nachvollziehbar | je nach Produkt | jeder Schritt steht im Chat, dazu signiertes Auditprotokoll mit Weitergabe an das eigene SIEM und Ablaufverfolgung im eigenen Haus |
| Kosten | Lizenz je Nutzer | Hardware und Betrieb, keine Lizenz je Nutzer |
| Einbindung in Office | ja | nein |
| Reife | Produkt mit Support | Eigenentwicklung mit [offenen Punkten](#was-bis-zum-einsatz-fehlt) |

Argus ist kein TISAX-Nachweis. Es bringt die technischen Eigenschaften mit, auf die es bei
Unterlagen mit hohem Schutzbedarf ankommt, und macht sie prüfbar: Der Quelltext liegt offen,
die Netz-Abschottung wird bei jeder Änderung automatisch kontrolliert, und Sperren,
Anmeldungen und Kontoänderungen stehen im Auditprotokoll, das an das SIEM der Firma gehen
kann ([Warum Argus sicher ist](#warum-argus-sicher-ist)).

## Was heute geht

- **Fragen an eigene Dokumente:** PDF, Word, Excel, PowerPoint, EPUB, HTML, Text. Das Modell
  antwortet aus den gefundenen Textstellen und nicht aus dem Gedächtnis (oft „RAG“ genannt).
  Die Suche kombiniert Bedeutung (bge-m3) und Stichworte (BM25) und bewertet die Treffer mit
  einem Reranker. Kennungen wie `RK-2025-0041` oder Teilenummern trifft sie wörtlich.
  Über der Antwort steht, in welchen Dokumenten die Textstellen gefunden wurden. Diese Angabe
  kommt aus der Suche, nicht aus dem Text des Modells, kann also nicht erfunden sein.
- **Web-Recherche** mit Quellen über eine eigene Suchinstanz, getrennt von Gesprächen mit
  Firmendaten ([Absicherung](#warum-websuche-und-wie-sie-abgesichert-ist)).
- **Datei in den Chat ziehen oder anhängen:** Eine PDF, eine Tabelle oder ein anderes Dokument
  landet im eigenen Bereich des Kontos, Argus liest es und arbeitet damit. Ein Gespräch mit
  beigelegter Datei gilt sofort als Gespräch mit Firmendaten. Jedes Konto sieht nur seine
  eigenen Dateien, auch Administratoren sehen die Dateien anderer nicht.
- **Rechnen und Auswerten** in einer abgeschotteten Sandbox ohne Netz (Python, Tabellen). Sie
  bekommt nur die Dateien des eigenen Kontos.
- **Ergebnisse herunterladen:** Berichte, Tabellen und andere Dateien, die Argus erzeugt,
  stehen über der Antwort zum Herunterladen bereit und unter *Meine Dateien*. Dateien und
  Ergebnisse werden nach sieben Tagen gelöscht (`USER_FILES_RETENTION_HOURS`), je Konto stehen
  500 MB zur Verfügung (`USER_FILES_MAX_MB`).
- **Sprache:** Spracherkennung (Whisper) und Sprachausgabe, beides lokal.
- **Konten und Rollen:** Administratoren verwalten Dokumente und Konten und sehen den Monitor.
  Benutzer fragen, suchen in den Dokumenten und recherchieren. Argus spricht jede Person mit
  ihrem Namen an.
- **Nachvollziehbarkeit:** Jeder Werkzeugschritt steht im Chat, jede Anfrage als Ablauf in
  Phoenix (im eigenen Haus, Inhalte geschwärzt). Sicherheitsrelevante Ereignisse wie
  Anmeldungen, Kontoänderungen und Sperren stehen im signierten Auditprotokoll und gehen auf
  Wunsch an das SIEM der Firma.

![Web-Recherche in einem eigenen Gespräch: Suchen, gelesene Seiten, Antwort mit Quellen](docs/images/recherche.png)

![Ein Benutzer hängt eine Tabelle an: Argus liest sie, wertet sie in der Sandbox aus und legt das Ergebnis als CSV zum Herunterladen ab](docs/images/datei-auswertung.png)

![Monitor für Administratoren: Durchsatz, Auslastung, Werkzeugaufrufe, Dokumente, Auditprotokoll](docs/images/monitor.png)

*Die Aufnahmen zeigen den Stand dieses Repos mit dem Modell Qwen3.6-35B-A3B. Dokumente,
Firmen und Namen darin sind erfunden (`eval/retrieval/corpus`).*

## Gemessen

Alle Werte stammen vom selben Rechner: ein PC mit einer Grafikkarte (RTX 5070 Ti, 16 GB),
Modell Qwen3.6-35B-A3B. Die Messungen liegen im Repo und lassen sich wiederholen.

| Was | Wert | Wie gemessen |
|---|---|---|
| Dokumentensuche: richtiges Dokument an erster Stelle | 96 % (vor dem Umbau 67 %) | 60 Fragen an 30 Dokumente, `eval/retrieval` |
| Dauer einer Dokumentensuche | 3,5 s (vorher 19,9 s) | ebenda, Suche und Bewertung auf der CPU |
| Aufgaben, die der ganze Agent richtig löst | 91 von 96 (mit Gemma 4: 94 von 96, [Vergleich](#das-modell-ist-austauschbar)) | 32 Aufgaben mit je 3 Durchläufen gegen den echten Agenten, `eval/agent` |
| Dauer je Aufgabe | 24 s im Median | ebenda |
| Schreibtempo des Modells | 79 Token je Sekunde, bei zwei Anfragen gleichzeitig je 47 | `bench.sh` im Container |
| Text, den eine Anfrage fassen kann | 72.000 Token, rund 100 Seiten | Einstellung `LLM_CTX_PER_SLOT` |
| Automatische Tests | 327 für das Backend, 36 für die Rechen- und Code-Sandbox | bei jeder Änderung im CI, zusammen mit der Prüfung der Netz-Abschottung |

Die Aufgaben der Agenten-Messung prüfen unter anderem, ob Argus interne Fragen aus den
Dokumenten beantwortet statt aus dem Gedächtnis, ob es Internes nicht im Web sucht und ob es
sich von eingeschleusten Anweisungen in Dokumenten nicht lenken lässt.

## Warum Argus sicher ist

Nicht, weil sich das Modell gut benimmt, sondern weil es nichts Gefährliches tun kann. Ein
Sprachmodell lässt sich mit geschickt formuliertem Text (Prompt Injection) zu fast allem
überreden, auch über eine präparierte Webseite oder ein präpariertes Dokument. Argus geht
deshalb davon aus, dass das Modell irgendwann falsch handelt, und sorgt dafür, dass daraus
kein Schaden entsteht. Die Schutzmaßnahmen sitzen außerhalb des Modells:

| Argus ist sicher, weil … | So ist es umgesetzt | Prüfbar über |
|---|---|---|
| das Modell nichts hinausschicken kann | Es läuft in einem Netz ohne Ausgang. Ins Internet kommen nur das Backend und die eigene Suchinstanz. | `setup/check_networks.py`, bei jeder Änderung im CI |
| Firmendaten und Internet nie im selben Gespräch zusammenkommen | Hat ein Gespräch Firmendaten gelesen oder eine Datei bekommen, ist darin jeder Weg nach außen gesperrt. | Tests, Agenten-Messung (Gruppe Sicherheit) |
| jede Person nur ihr Eigenes sieht | Rollen werden an jeder Route im Backend geprüft. Dateien liegen je Konto getrennt, die Code-Sandbox bekommt nur die Dateien des eigenen Kontos. | Tests (`test_user_files.py`) |
| das Modell auf keinem Rechner etwas ausführen kann | Kein Zugang zu Rechnern, keine Zugangsdaten in den Containern. Code läuft nur in einer Sandbox ohne Netz, die nach jedem Lauf alles löscht. | Compose-Datei, Tests der Sandbox |
| fremde Inhalte als Daten gelten, nicht als Anweisung | Webseiten und Dokumente laufen durch eine Prüfung auf eingeschleuste Anweisungen. Köder-Schlüssel schlagen Alarm, wenn etwas Geschütztes hinaus will. | Agenten-Messung (Injection in Dokument und Webseite) |
| sich alles nachvollziehen lässt | Signiertes Auditprotokoll, auf Wunsch weitergegeben an das SIEM der Firma ([unten](#wer-hat-was-getan-auditprotokoll-und-siem)). | `GET /v1/dashboard/audit/verify`, das SIEM |
| man es nicht glauben muss | Quelltext offen, Tests und Messungen liegen im Repo und lassen sich wiederholen. | dieses Repo |

Die Einzelheiten stehen im [Sicherheitskonzept](#sicherheitskonzept). Was noch fehlt, steht
unter [Offen für einen Einsatz im Unternehmen](#offen-für-einen-einsatz-im-unternehmen), vor
allem: Wer den Server bedient, kann die Dateien lesen, und eine Anmeldung über das
Firmenverzeichnis (SSO) gibt es noch nicht.

### Wer hat was getan: Auditprotokoll und SIEM

**Was ist ein SIEM?** Ein SIEM (Security Information and Event Management) ist die zentrale
Stelle, an der die IT-Sicherheit die Protokolle aller Systeme sammelt: Firewall,
Verzeichnisdienst, Virenschutz auf den Rechnern, Server. Dort werden sie unveränderlich
aufbewahrt, miteinander verknüpft und automatisch ausgewertet. Typische Regeln sind viele
Fehlanmeldungen in kurzer Zeit, eine Anmeldung zu ungewöhnlicher Uhrzeit oder ein neues
Administratorkonto. Schlägt eine Regel an, bekommt das Sicherheitsteam einen Alarm. Verbreitet
sind etwa Microsoft Sentinel, Splunk, IBM QRadar, CrowdStrike Falcon Next-Gen SIEM oder das
freie Wazuh.

**Warum das zählt.** Ein neues System, das vertrauliche Unterlagen verarbeitet, muss sich in
diese Überwachung einfügen. Der VDA-ISA-Katalog, auf dem TISAX beruht, verlangt, dass
sicherheitsrelevante Ereignisse protokolliert, vor Veränderung geschützt und ausgewertet
werden. Ein Protokoll, das nur auf dem Server selbst liegt, erfüllt das schlecht: Wer den
Server übernimmt, kann es löschen.

**Was Argus protokolliert:**

| Ereignis | Beispiele | Wofür im SIEM |
|---|---|---|
| Anmeldung | erfolgreich, falsches Passwort, unbekanntes Konto, gesperrt nach Fehlversuchen, Abmeldung | Passwort-Raten erkennen, Anmeldungen mit anderen Systemen abgleichen |
| Kontoänderung durch einen Administrator | Konto angelegt oder gelöscht, Rolle geändert, Passwort zurückgesetzt | Rechteausweitung erkennen: Wer hat wen wann zum Administrator gemacht? |
| Sperre nach Firmendaten | Websuche in einem Gespräch mit Firmendaten abgewiesen | Eine Häufung zeigt Fehlbedienung oder einen Angriffsversuch über präparierte Inhalte |
| Köder-Alarm | ein Köder-Schlüssel sollte hinaus | höchste Stufe: deutet auf einen gezielten Abflussversuch |
| Schreiben im Arbeitsordner | Datei über ein Werkzeug der Administratoren angelegt oder geändert, mit Pfad | Änderungen an Dateien nachvollziehen |
| Freigaben und Cloud-Aufrufe (nur mit Cloud-Modul) | wer was freigegeben hat, was an ein Cloud-Modell ging | nachvollziehen, was das Haus verlassen hat |
| Leeren des Protokolls | ein Administrator löscht die Einträge in der Datenbank | wird selbst gemeldet; im SIEM bleiben die alten Einträge erhalten |

Jeder Eintrag trägt Zeit, Ereignis und Ergebnis, dazu Konto und Adresse, wo sie bekannt sind.
**Nicht im Protokoll** stehen Passwörter, Chat-Verläufe und Dokumenttexte. Ein unbekannter
Kontoname wird nur als Prüfsumme gespeichert, weil in diesem Feld oft versehentlich ein
Passwort landet. Die einzige Ausnahme bei Inhalten: Was mit dem Cloud-Modul an ein
Cloud-Modell geht, steht als Anfrage im Protokoll, denn genau das soll nachvollziehbar sein.

**Schutz vor Manipulation.** Jeder Eintrag ist mit einem geheimen Schlüssel signiert und mit
dem vorigen verkettet (HMAC-Kette). Wer einen Eintrag ändert, löscht oder umsortiert, bricht
die Kette, und die Prüfung meldet die Stelle. Endgültig sicher ist das Protokoll erst im
SIEM, denn darauf hat ein Administrator von Argus keinen Zugriff.

**Einschalten** in der `.env`, zum Beispiel für einen Syslog-Empfänger des SIEM:

```ini
AUDIT_SIEM=syslog
AUDIT_SIEM_FORMAT=cef
AUDIT_SYSLOG_HOST=siem-collector.firma.local
AUDIT_SYSLOG_PROTOCOL=tcp
```

Argus sendet nach RFC 5424 über UDP oder TCP, als JSON oder als CEF (Common Event Format),
das gängige SIEM-Produkte direkt einlesen. Alternativ schreibt `AUDIT_SIEM=stdout` jeden
Eintrag ins Container-Log, wo ein vorhandener Log-Sammler ihn abholt. Die Weitergabe hält
keine Anfrage auf. Ist das SIEM nicht erreichbar, zählt Argus die nicht zugestellten Einträge
und warnt. Alle Schalter stehen unter [Sicherheits-Schalter](#sicherheits-schalter).

**Für den Betrieb:** Das Protokoll enthält Anmeldungen einzelner Personen. Wie andere
Protokolle gehört es deshalb ins Löschkonzept und, wo es einen Betriebsrat gibt, in die
passende Betriebsvereinbarung. Argus löscht Audit-Einträge nicht von selbst; wie lange sie
aufbewahrt werden, regelt das SIEM.

## Warum Websuche, und wie sie abgesichert ist

Das Wissen eines Sprachmodells endet mit seinem Training. Normen, Vorschriften,
Herstellerangaben und alles Aktuelle kennt es nicht oder nur veraltet, und ohne Zugriff auf
Quellen neigt es dazu, Lücken zu füllen. Mit der Websuche belegt Argus solche Antworten mit
Quellen. Dabei gilt:

- **Das Modell selbst hat kein Internet.** Es läuft in einem Netz ohne Ausgang. Suchen und
  Seiten abrufen kann nur das Backend.
- **Eigene Suchinstanz.** Gesucht wird über SearXNG im eigenen Stapel, ohne Konto und ohne
  Profil bei einer Suchmaschine. Bei einer Recherche gehen die Suchbegriffe und die Adressen
  der abgerufenen Seiten nach außen.
- **Gespräche mit Firmendaten können nicht suchen.** Hat ein Gespräch ein Dokument oder eine
  Datei gelesen oder wurde ihm eine Datei beigelegt, sind Websuche, Seitenabruf und alle
  anderen Wege nach außen darin gesperrt. Ein abgewiesener Versuch steht mit einem Schloss
  im Chat.
  Das verhindert, dass eine präparierte Webseite oder ein präpariertes Dokument das Modell
  dazu bringt, Inhalte über eine Suchanfrage hinauszutragen.
- **Internes wird nicht im Web gesucht.** Eine feste Regel schickt Fragen nach Richtlinien,
  Verträgen, Preisen und Datenblättern zuerst in die Dokumente.
- **Fremde Inhalte werden geprüft.** Jede geladene Seite läuft durch eine Prüfung auf
  eingeschleuste Anweisungen. Der Seitenabruf erreicht keine internen Adressen, auch nicht
  über Weiterleitungen.
- **Köder-Schlüssel.** Taucht einer der hinterlegten Köder in einer Suchanfrage oder Adresse
  auf, wird die Anfrage blockiert und im Audit vermerkt.

Was bleibt: Was jemand selbst in ein Recherche-Gespräch tippt, kann als Suchbegriff
hinausgehen, wie bei einer Suche von Hand. Einen Schalter, der die Websuche ganz abstellt,
gibt es noch nicht.

## Das Modell ist austauschbar

Argus hängt an keinem bestimmten Modell. Der Modell-Dienst lädt eine Modelldatei (GGUF), das
Backend spricht ihn über eine genormte Schnittstelle an. Ein Wechsel heißt: andere Datei in
den Modellordner, zwei Zeilen in der `.env` (`LLM_MODEL_FILE`, `OAI_MODEL`). Welches Modell
eine Firma zulässt, ist damit ihre Entscheidung, etwa nach Herkunft oder Lizenz.

| Modell | Lizenz | Bilder als Eingabe | Anmerkung |
|---|---|---|---|
| Qwen3.6-35B-A3B (Alibaba) | Apache 2.0 | ja | Damit sind alle Werte oben gemessen. |
| Gemma 4, 26B-A4B oder 31B (Google) | Apache 2.0 | ja | 26B-A4B gemessen, siehe unten |
| Mistral Small 3.2, 24B (Mistral AI, Frankreich) | Apache 2.0 | ja | europäischer Anbieter |
| Llama 4 (Meta) | Llama 4 Community License | ja | Die Nutzungsbedingungen nehmen Firmen mit Hauptsitz in der EU von den Rechten an den multimodalen Modellen aus. Vor einem Einsatz rechtlich prüfen. |

Lizenzangaben nach den Modellkarten der Anbieter, Stand November 2026; vor einem Einsatz
prüfen. Gemessen sind Qwen3.6-35B-A3B und Gemma 4 26B-A4B.

**Wie gut ist ein Modell?** Das sagt bei Argus eine Messung und keine Behauptung: Die
Agenten-Messung (`eval/agent`) läuft in etwa einer Stunde gegen ein beliebiges Modell und
liefert dieselbe Tabelle wie oben. So lässt sich ein Ersatzmodell vor dem Wechsel mit dem
bisherigen vergleichen. Ein Modell dieser Größe ist schwächer als die großen Cloud-Modelle,
vor allem bei langen Aufgaben mit vielen Schritten. Für Fragen an Dokumente,
Zusammenfassungen und Recherche mit Quellen reicht es, wie die Werte oben zeigen.

**Gemessen: Qwen gegen Gemma.** Beide auf derselben Karte (16 GB), beide in Q4 (Unsloth
UD-Q4_K_XL), beide mit MTP, mit denselben Prompts und derselben Agenten-Messung (32 Aufgaben,
je 3 Durchläufe). Die Prompts sind an Qwen entwickelt und für Gemma nicht angepasst.

| | Qwen3.6-35B-A3B | Gemma 4 26B-A4B |
|---|---|---|
| Aufgaben richtig gelöst | 91 von 96 | 94 von 96 |
| davon zurückgehaltene Kontrollaufgaben | 18 von 18 | 18 von 18 |
| Dauer je Aufgabe (Median) | 24 s | 36 s |
| Schreibtempo mit MTP | 79 Token/s | 89 Token/s |
| Einlesen | ~2.800 Token/s | ~3.600 Token/s |
| Modelldatei | 22,9 GB | 17,0 GB, dazu 0,5 GB für MTP |
| VRAM, Experten im RAM | 14,9 GB, 25 von 40 Schichten | 14,8 GB, 14 von 30 Schichten |

Beide reichen für den Einsatzzweck. Ein Unterschied von drei Durchläufen liegt nahe am
Rauschen dieser Messung; belastbar ist, dass der Wechsel ohne eine Zeile Code und ohne
angepasste Prompts ein gleich gutes Ergebnis liefert. Gemma löst die Aufgaben, an denen Qwen
scheitert (Kilometerpauschale aus der Richtlinie statt der gesetzlichen, Wochentag,
Vergleichstabelle). Schwächer ist es bei der Recherche: Es sucht auch öffentliche Fragen
zuerst in den Firmendokumenten, damit gilt das Gespräch als Gespräch mit Firmendaten und die
Websuche ist gesperrt. Je Aufgabe braucht es länger, obwohl es pro Token schneller schreibt;
vermutlich denkt es länger nach, einzeln gemessen ist das nicht.

**Sampling gehört zum Wechsel.** Mit den von Google empfohlenen Werten (Temperatur 1,0, top_k
64) kam Gemma auf 92 von 96, im Median 47 s, und einmal brach eine Aufgabe nach 600 s ab,
weil sich das Modell im Denken im Kreis drehte. Mit den ruhigeren Werten, die Argus für Qwen
nutzt, und einer Strafe für Wiederholungen ergab sich die Tabelle oben. Bei einem neuen
Modell also beides messen.

Gemma 4 einstellen (`.env`), Sampling-Werte leer lassen:

```ini
LLM_MODEL_FILE=gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf
LLM_MTP_MODEL_FILE=mtp-gemma-4-26B-A4B-it.gguf
LLM_N_CPU_MOE=14
OAI_MODEL=gemma-4-26b-a4b
```

Beide Dateien liegen bei Hugging Face unter `unsloth/gemma-4-26B-A4B-it-GGUF`.

**Ein Modell für Text und Bild.** Alle Modelle in der Tabelle sind multimodal
(Vision-Language): Sie lesen Text und Bilder mit denselben Gewichten. Eine gescannte Seite, ein
Foto vom Prüfteil oder eine Zeichnung liest also dasselbe Modell, das danach die Frage
beantwortet oder den Bericht bewertet. Ein eigener OCR-Schritt und ein zweites Modell nur für
Bilder sind dafür nicht nötig, und die Karte muss nur ein Modell im Speicher halten statt
zwei. Für den Bildteil lädt der Modell-Dienst eine kleine Zusatzdatei (mmproj), die bei
Hugging Face neben dem Modell liegt: 0,9 GB bei Qwen3.6, 1,2 GB bei Gemma 4.

Stand in Argus: Oberfläche und Backend geben Bilder schon an das Modell weiter
(`LLM_ENABLE_MULTIMODAL`). Der Modell-Dienst lädt die mmproj-Datei noch nicht, Bildeingabe ist
deshalb aus und nicht gemessen. PDFs ohne Textebene, also reine Scans, nimmt die
Dokumentensuche heute nicht auf. Ob ein Modell dieser Größe schlechte Scans, Handschrift oder
dichte Tabellen so sicher liest wie ein eigenes OCR-Modell, muss eine Messung mit echten Seiten
zeigen, so wie die Agenten-Messung für Text.

## Was bis zum Einsatz fehlt

Argus läuft und ist gemessen, aber bisher auf einem einzelnen Rechner. Für einen Betrieb mit
vielen Personen fehlen vor allem:

- **Anmeldung über das Firmenverzeichnis (SSO)** statt eigener Konten.
- **Rechte der Quellen:** Die Wissensbasis ist für alle Konten dieselbe.
- **Bilder und Scans:** Die Modelle können Bilder lesen, in Argus ist das noch nicht
  eingeschaltet. Gescannte PDFs ohne Textebene werden nicht aufgenommen
  ([Text und Bild](#das-modell-ist-austauschbar)).
- **Betrieb:** ein TLS-Proxy und ein Modell-Dienst für viele gleichzeitige Nutzer. Eine
  geführte Installation gibt es (siehe [Einrichten](#einrichten)).

Die vollständige Liste steht unter
[Offen für einen Einsatz im Unternehmen](#offen-für-einen-einsatz-im-unternehmen).

## Aufbau des Repos

| Ordner | Inhalt |
|---|---|
| `rag_backend/` | Backend: FastAPI, LangGraph-Agent, Dokumentensuche, Freigaben, Audit |
| `rag_backend/static/` | Browser-Oberfläche (reines HTML, CSS, JavaScript, ohne Build-Schritt) |
| `alembic/` | Datenbank-Migrationen |
| `docker-compose.yml` | der Stapel |
| `setup/` | Einrichtung (`init_stack.py`), Prüfung der Netze (`check_networks.py`) |
| `stt_service/`, `tts_service/`, `calc_sandbox/` | Sprache und abgeschottete Code-Ausführung |
| `local_proxy/`, `searxng_config/` | die beiden Proxys auf 127.0.0.1, Websuche |
| `eval/` | Messungen: Dokumentensuche (`retrieval/`) und ganzer Agent (`agent/`) |

## Architektur

```mermaid
flowchart LR
  subgraph Clients
    WEB["Browser<br/>Oberfläche unter /dashboard"]
    API2["weitere Clients<br/>OpenAI-kompatible API"]
  end
  subgraph Docker["Docker, Ports nur auf 127.0.0.1"]
    subgraph EXT["Netz backend, mit Ausgang"]
      API["rag-backend<br/>FastAPI · LangGraph · Oberfläche"]
      SX["searxng"]
      PX["local-proxy<br/>nginx, Host-Prüfung"]
    end
    subgraph INT["Netz intern, ohne Ausgang"]
      LLM["llm<br/>llama.cpp · Sprachmodell"]
      EMB["embed · rerank<br/>bge-m3"]
      DB[("postgres · qdrant")]
      PH["phoenix<br/>Ablaufverfolgung"]
      SP["stt · tts"]
    end
    IP["intern-proxy<br/>nginx, eigenes Netz"]
    SB["calc-sandbox<br/>Netz ohne Ausgang"]
  end
  WEB -->|"Anmeldung, Chat (SSE)"| API
  API2 -->|"mit API-Key"| IP
  IP --> LLM
  IP --> EMB
  IP --> SP
  IP --> PH
  PX --> SX
  API --> LLM
  API --> DB
  API --> SB
  API --> SX
  SX -->|Websuche| NET((Internet))
  API -->|Seitenabruf| NET
  API -.->|"optional, pseudonymisiert"| CLOUD((Cloud-Modelle))
```

## Sicherheitskonzept

**Grundsatz:** Das Modell ist ein nicht vertrauenswürdiger Baustein. Prompt Injection lässt
sich im Modell nicht zuverlässig verhindern. Deshalb hängt die Sicherheit an Rechten,
Freigaben und Abschottung, nicht daran, dass sich das Modell richtig verhält.

**Kein Weg auf einen Rechner.** Argus kann nichts auf einem Computer ausführen: Es gibt
kein Admin-Konto, keine Zugangsdaten für einen Host in einem Container und keinen Schalter,
der das ändert. Code läuft nur in der Sandbox ohne Netz.

**Zwei Rollen:**

| | Benutzer | Administrator |
|---|---|---|
| Fragen, Dokumente durchsuchen, im Web recherchieren, rechnen, Sprache | ja | ja |
| Eigene Chats | nur die eigenen | nur die eigenen |
| Dokumente hochladen, in die Wissensbasis aufnehmen, löschen | nein | ja |
| Eigene Dateien in den Chat geben, auswerten, Ergebnisse herunterladen | ja | ja |
| Dateien anderer Konten | nein | nein |
| Konten anlegen, Monitor, Audit, Ablaufverfolgung | nein | ja |
| Cloud-Modelle (nur mit Cloud-Modul) | nein | ja |

**Schichten:**
- **Netze:** Ins Internet kommen nur `rag-backend` (Websuche, Seitenabruf, optional Cloud) und
  SearXNG. Modell, Einbettung, Reranker, Datenbanken, Sprache und Phoenix hängen im Netz
  `intern` ohne Ausgang. Sie erreichen weder das Internet noch SearXNG, laden also nichts nach
  und können nichts hinausschicken, auch wenn einer von ihnen kompromittiert wäre.
  `intern-proxy` leitet nur an Ziele in `intern` weiter und teilt sein Netz zum
  Veröffentlichen der Ports mit keinem anderen Dienst, damit er keine Brücke wird. Die
  Code-Ausführung hat ein eigenes Netz ohne Ausgang. `setup/check_networks.py` prüft diese
  Regeln bei jedem Push im CI.
- **Sperre nach Firmendaten:** Gefährlich wird ein Agent, der zugleich Firmendaten liest,
  fremde Inhalte verarbeitet und nach außen senden kann. Dann kann eine präparierte Seite ihn
  dazu bringen, Daten über eine Suchanfrage abfließen zu lassen. Liest ein Gespräch
  Firmendaten, ist darin jeder Weg nach außen gesperrt (`EGRESS_AFTER_SENSITIVE=block`):
  Websuche, Seitenabruf, Wetter, Routen, Cloud-Modelle, Missionen und fremde MCP-Werkzeuge.
  Als Firmendaten zählen Dokumentensuche und -abruf, Dateien und die Code-Sandbox (sie bekommt
  die Dateien des Kontos). Eine beigelegte Datei zählt schon, bevor sie gelesen ist: Nennt
  eine Nachricht eine Datei aus dem eigenen Bereich, gilt das Gespräch von da an als
  Gespräch mit Firmendaten. Abgewiesene Schritte stehen mit einem Schloss im Chat. Die
  Sperre gilt auch für Unteragenten und bleibt nach einem
  Neustart bestehen. Jede Sperre landet im Audit, ohne Inhalte. Für eine
  Recherche beginnt man ein neues Gespräch.
- **Fremde Inhalte:** Websuche und Seitenabruf laufen durch eine Injection-Prüfung
  (`WEB_INJECTION_GUARD`). Der Seitenabruf sperrt interne Adressen, auch nach Weiterleitungen,
  und liest höchstens 3 MB je Seite (`WEBPAGE_MAX_BYTES`).
- **Rollen:** Geprüft wird an jeder Route im Backend, nicht in der Oberfläche. Benutzer
  bekommen die Werkzeuge für Cloud, Unteragenten und die Wissensbasis gar nicht erst
  angeboten, und der Prompt beschreibt sie ihnen nicht.
- **Bereich je Konto:** Hochgeladene Dateien und Ergebnisse liegen unter
  `users/<Konto>/Dateien` und `users/<Konto>/Ergebnisse` im Arbeitsordner. Jeder Zugriff
  (Oberfläche, Download, Werkzeuge) läuft über eine Stelle (`rag_backend/user_files.py`), die
  Namen bereinigt, den Pfad auf genau diesen Ordner festlegt und Verknüpfungen ablehnt. Auch
  Administratoren erreichen über ihre Werkzeuge keine fremden Bereiche. Die Code-Sandbox
  bindet den Arbeitsordner nicht mehr ein: Das Backend schickt ihr nur die Dateien des Kontos,
  sie führt die Läufe nacheinander aus und löscht danach alles, auch zurückgebliebene
  Prozesse.
- **Freigaben:** Mit dem Cloud-Modul fragt eine Mission vor dem Versand per Karte im Chat
  nach. Freigeben kann nur das Konto, das sie ausgelöst hat.
- **Anmeldung:** Sitzung als HttpOnly-Cookie, Passwörter mit Argon2, keine
  Selbstregistrierung, Sperre nach fünf Fehlversuchen. Abmelden und Passwortwechsel beenden
  alle Sitzungen des Kontos. Das erste Administrator-Konto legt nur an, wer den
  Einrichtungs-Schlüssel der Installation kennt (`DASHBOARD_SETUP_TOKEN`).
- **Audit:** Ein Protokoll mit HMAC-Kette hält fest, was sicherheitsrelevant ist:
  Werkzeugaufrufe mit Schreibzugriff, Sperren nach Firmendaten, Köder-Alarme, Freigaben,
  Anmeldungen (erfolgreich, fehlgeschlagen, gesperrt), Abmeldungen und jede Kontoänderung
  durch einen Administrator (angelegt, Rolle, Passwort, gelöscht). Gespeichert werden Konto,
  Adresse und Grund, nie Passwörter oder Inhalte. Bei einem unbekannten Kontonamen steht nur
  ein Hash im Protokoll, weil dort oft ein Passwort im falschen Feld landet. Jede Änderung oder
  Lücke in der Kette meldet `GET /v1/dashboard/audit/verify` (nur für Administratoren). Mit
  `AUDIT_SIEM` geht jeder Eintrag zusätzlich an ein SIEM (siehe
  [Sicherheits-Schalter](#sicherheits-schalter)).
- **Oberfläche:** Die Seite lädt nichts von fremden Hosts (Schriften und Skripte liegen im
  Repo). Content-Security-Policy, Prüfung von Host- und Origin-Header gegen DNS-Rebinding und
  CSRF. Text des Modells wird vor der Anzeige maskiert.
- **Köder-Schlüssel** (`CANARY_TOKENS`) in einer nie gebrauchten Datei. Taucht einer in einer
  Cloud-Anfrage, Websuche, URL oder in Werkzeug-Parametern auf, wird blockiert und gewarnt.
- **Ablaufverfolgung:** Phoenix bekommt Prompts und Antworten nur geschwärzt (Schlüssel,
  Tokens, E-Mail, IBAN, Telefon, interne Adressen, Begriffe der PII-Liste). Mit
  `TRACE_CONTENT=metadata` gehen nur Ablauf, Dauer, Token-Zahlen und Werkzeugnamen hin.
  Scheitert die Schwärzung, wird nichts gesendet.
- **Cloud-Modelle** sind aus (`CLOUD_AGENTS_ENABLED=false`). Eingeschaltet bekommen sie nur
  geschwärzte Texte, ein PII-Stopp blockiert, wenn die Schwärzung nicht greift, und ohne
  `CLOUD_MISSION_AUTO_APPROVE=true` fragt jeder Versand nach.
- **Container:** Ports nur auf `127.0.0.1`, `cap_drop: ALL`, `no-new-privileges`, Speicher- und
  Prozessgrenzen (das Modell bewusst ohne Speichergrenze, siehe Compose). Die Code-Ausführung
  hat keine Einbindung des Arbeitsordners und keine Geheimnisse. Basis-Images sind per Digest festgelegt, Migrationen
  schreibgeschützt eingebunden.
- **Geheimnisse** liegen nie im Repo, sondern in `API_Tokens/` mit Rechten nur für den Besitzer.

### Offen für einen Einsatz im Unternehmen

Diese Punkte sind bekannt und noch nicht gelöst. Wer Argus mit mehreren Personen betreibt,
sollte sie kennen.

- **Dateien bleiben lesbar für den Server:** Die Bereiche je Konto trennen die Konten in
  Argus. Wer den Server oder Docker bedienen darf, liest sie trotzdem. Eine Verschlüsselung je
  Konto gibt es nicht.
- **Rechte der Quellen:** Die Dokumentensuche kennt eine gemeinsame Wissensbasis. Für Firmen
  müsste sie die Rechte der Quelle mitprüfen (etwa SharePoint). Die Anmeldung läuft über
  eigene Konten, nicht über SSO (OIDC).
- **Cloud-Missionen:** Erkennt der Router eine Nachricht eines Administrators als
  Hintergrund-Auftrag, startet er die Mission ohne Karte im Chat (Schwärzung und PII-Stopp
  laufen trotzdem). Betrifft nur Installationen mit Cloud-Modul.
- **Langzeitgedächtnis:** Die Zusammenfassung je Konto wird im normalen Chat derzeit nicht
  fortgeschrieben.
- **Name in der Anrede:** Er leitet sich aus dem Kontonamen ab; mit SSO käme er aus dem
  Verzeichnis.
- **Audit:** Ein Administrator kann das Protokoll in der Datenbank leeren. Das Leeren wird
  vermerkt und mit `AUDIT_SIEM` auch ans SIEM gemeldet. Unveränderlich ist das Protokoll erst
  dort. Was das SIEM nicht erreicht, wird nicht nachgeliefert.
- **Betrieb:** Der Stapel bringt keinen TLS-Proxy mit. Hinter einem Proxy teilen sich alle
  Nutzer ein Ratenlimit, weil nach Adresse gezählt wird. Wer Docker auf dem Server bedienen
  darf, hat dort praktisch Admin-Rechte.
- **Lizenzen als Module:** Websuche (SearXNG, AGPL) und Ablaufverfolgung (Phoenix, Elastic
  License) gehören fest zum Stapel. Für ein Angebot an Dritte sollten beide abschaltbar oder
  austauschbar sein.
- **Lieferkette:** Python-Pakete haben keine Lock-Datei, drei Images installieren Pakete ohne
  feste Version. Die Modelle prüft die Installation per SHA-256, wer sie von Hand ablegt,
  prüft selbst.
- **Installation unter Linux:** `install.sh` ist nur in Teilen getestet (Syntax,
  ShellCheck, die Schritte in Python unter Windows), noch nicht auf einem Linux-Server.
- **Bilder und Scans:** Oberfläche und Backend können Bilder an das Modell weitergeben
  (`LLM_ENABLE_MULTIMODAL`). Der Modell-Dienst lädt die dafür nötige Zusatzdatei (mmproj)
  noch nicht; Bildeingabe ist aus und nicht getestet. PDFs ohne Textebene (Scans) überspringt
  die Aufnahme in die Dokumentensuche. Der vorgesehene Weg ist das multimodale Modell selbst,
  kein eigenes OCR.
- **Websuche abschalten:** Einen Schalter, der die Websuche ganz abstellt, gibt es nicht.
- **Grenzen der Sperre:** Vom Nutzer selbst eingefügte Bilder und Texte zählen nicht als
  Firmendaten. Der Modus `allow` vergleicht nur Fingerabdrücke (Kennungen, E-Mail, IBAN,
  Wortfolgen). Umkodierte Daten (etwa Base64) erkennt er nicht, er ist also kein Schutz gegen
  einen gezielten Angriff.

## Einrichten

Getestet unter Windows 11 mit Docker Desktop (WSL2). Linux mit Docker Engine und dem NVIDIA
Container Toolkit ist vorgesehen, aber noch nicht auf echter Hardware durchgetestet.

**Voraussetzungen:** eine NVIDIA-Karte (gemessen mit 16 GB), 32 GB Arbeitsspeicher, rund
30 GB Platz für die Modelle.

### Geführte Installation

Unter Windows `install.cmd` per Doppelklick starten, unter Linux `./install.sh`. Die
Installation führt durch diese Schritte:

1. **Python** ab 3.10 suchen. Fehlt es, fragt sie und installiert Python 3.13 über `winget`
   (Windows) oder `apt-get` (Debian, Ubuntu).
2. **Docker** prüfen. Unter Windows bietet sie an, Docker Desktop und WSL über `winget` zu
   installieren, und startet Docker Desktop, wenn es nicht läuft. Unter Linux nennt sie die
   Anleitung und prüft das NVIDIA Container Toolkit, installiert aber nichts selbst.
3. **Grafikkarte und Arbeitsspeicher** prüfen und vor zu wenig davon warnen.
4. **Fragen:** Arbeitsordner, Modellordner, Betrieb nur auf diesem Rechner oder als Server
   (dann mit Adresse, siehe [Betrieb auf einem Server](#betrieb-auf-einem-server)).
5. **Geheimnisse und `.env`** anlegen (`setup/init_stack.py`, in einer eigenen Umgebung
   `.venv-setup`). Ausgegeben werden nur Dateinamen, keine Werte.
6. **Modelle laden** (rund 25 GB): jede Datei aus `setup/models.json`, festgelegt auf eine
   Revision bei Hugging Face und nach dem Laden per SHA-256 geprüft. Abgebrochene Downloads
   setzen beim nächsten Lauf fort. Vorhandene Dateien werden nur geprüft.
7. **Stapel bauen und starten** und warten, bis Argus antwortet. Am Ende kopiert sie unter
   Windows auf Wunsch den Einrichtungs-Schlüssel in die Zwischenablage und öffnet den Browser.

Jeder Schritt fragt vorher. Ein zweiter Lauf ist harmlos: Vorhandenes bleibt, wie es ist.
Optionen: `--yes` (alle Vorgaben annehmen), `--workspace`, `--models`, `--server-url`,
`--no-models`, `--no-start`, `--models-only` (nur Modelle prüfen und laden).

`install.cmd` ist ein reines Batch-Skript ohne PowerShell und startet `setup/install.py`. Eine
EXE gibt es bewusst nicht: Ein lesbares Skript lässt sich prüfen, und unsignierte EXE-Dateien
fallen bei Endpoint-Schutz wie CrowdStrike eher auf.

**Erstes Konto:** `http://127.0.0.1:7860` öffnen. Die Seite fragt nach Benutzername,
Passwort und dem Einrichtungs-Schlüssel (in der `.env` unter `DASHBOARD_SETUP_TOKEN`). Dieses
Konto wird Administrator und legt weitere Konten unter *Monitor › Konten* an.

**Dokumente:** Als Administrator Dateien im Monitor hochladen, dann unter *Monitor ›
Überblick* „Ingest starten“. Zum Ausprobieren eignen sich die 30 erfundenen Dokumente in
`eval/retrieval/corpus`.

### Von Hand

1. `python -m pip install cryptography`, dann `python setup/init_stack.py` (mit
   `--workspace` und `--models` für andere Ordner als `C:\Argus_Workspace` und
   `C:\Argus_Models`). Legt `API_Tokens/` und die `.env` an.
2. `.env` prüfen: Modell und `LLM_N_CPU_MOE` passend zur Grafikkarte.
3. Die Modelle aus `setup/models.json` nach `ARGUS_MODELS_DIR` legen (Repo, Revision, Datei
   und Prüfsumme stehen dort). Die Dienste hängen in einem Netz ohne Ausgang und laden selbst
   nichts nach.
4. `docker compose up -d --build`. Der erste Bau dauert, weil der Sprachdienst seine Modelle
   (4,4 GB) lädt und übersetzt.

## Betrieb auf einem Server

Für mehrere Personen läuft Argus auf einem Server, die Nutzer brauchen nur einen Browser.
Dafür gehört ein Reverse-Proxy mit TLS vor Port 7860 (im Stapel nicht enthalten). In der
`.env`:

| Schalter | Wert |
|---|---|
| `DASHBOARD_ORIGINS` | Adresse der Oberfläche, z. B. `https://argus.example.org` |
| `TRUSTED_HOSTS` | derselbe Name zusätzlich zu `127.0.0.1,localhost,rag-backend` |
| `DASHBOARD_TRACES_URL` | Adresse, unter der der Browser Phoenix erreicht; leer zeigt der Reiter nur einen Hinweis |

Der Proxy muss `X-Forwarded-Proto` setzen, damit das Sitzungs-Cookie als `Secure` gesetzt
wird. Für einen Server mit vielen gleichzeitigen Nutzern ist llama.cpp nicht die erste Wahl.
Das Backend spricht das Modell über eine OpenAI-kompatible Schnittstelle an (`OAI_BASE_URL`);
ein Betrieb mit vLLM oder SGLang ist damit vorgesehen, mit diesem Stand aber nicht getestet.
Die offenen Punkte oben gelten.

**Grafikkarte für einen Server** (gerechnet, nicht gemessen). Experten in den RAM auslagern
(`LLM_N_CPU_MOE`) kann llama.cpp; damit reichen auf dem PC 16 GB. Mit vLLM liegt das ganze
Modell im VRAM: Qwen3.6-35B-A3B braucht in FP8 rund 35 GB, in 4 Bit gut 20 GB, dazu den
Speicher für den Kontext (KV-Cache) und gut 1 GB für den Bildteil. Eine Karte mit 48 GB reicht
damit für FP8, eine mit 24 bis 32 GB für 4 Bit. Gemma 4 26B-A4B braucht etwa ein Viertel
weniger. Weil Text und Bild in einem Modell stecken, gilt das für beides.

## Dienste

| Dienst | Aufgabe |
|---|---|
| `rag-backend` | Oberfläche und API (Anmeldung, Chats, Uploads, Websuche, Sprache), Port 127.0.0.1:7860 |
| `llm` | llama.cpp mit dem Sprachmodell (GGUF); für Clients 127.0.0.1:30000 |
| `embed`, `rerank` | bge-m3 und bge-reranker-v2-m3 (GGUF) auf der CPU für die Dokumentensuche; für Clients 127.0.0.1:30001 und :30002 |
| `postgres`, `qdrant` | Chats und Konten; Abschnitte mit dichtem Vektor und BM25-Sparse-Vektor |
| `searxng` | Websuche; Clients erreichen sie über `local-proxy` auf `127.0.0.1:8888` |
| `phoenix` | lokale Ablaufverfolgung; UI und Empfang über `intern-proxy` auf `127.0.0.1:6006` |
| `stt-service`, `tts-service` | Spracherkennung (Whisper), Sprachausgabe; für Clients 127.0.0.1:8003 und :8002, mit API-Key |
| `local-proxy` | nginx vor SearXNG: lässt nur Anfragen an `localhost`/`127.0.0.1` durch, sonst 421 (Schutz gegen DNS-Rebinding) |
| `intern-proxy` | nginx vor den Diensten im Netz `intern`: Phoenix mit derselben Host-Prüfung, Modell, Einbettung, Reranker und Sprache als TCP-Durchreichung |
| `calc-sandbox` | abgeschottete Code-Ausführung |

Clients außerhalb des Browsers brauchen den Modell-Schlüssel aus `API_Tokens/llm_api_key.txt`.

## Stellschrauben (`.env`)

Das Modell ist eine GGUF-Datei und austauschbar (`LLM_MODEL_FILE`, `OAI_MODEL`). Gemessen ist
bisher nur Qwen3.6-35B-A3B; die Werte unten gelten für dieses Modell auf einer 16-GB-Karte.

- `LLM_N_CPU_MOE=25` – Schichten, deren Experten im RAM liegen (nur Modelle mit Experten,
  MoE). Mit MTP ~14,9 GB VRAM. Bei 16 lief der VRAM über (Windows lagert dann aus:
  ~3 Token/s, Absturz); unter ~15,3 GB bleiben.
- `LLM_CTX_SIZE=102400`, `LLM_PARALLEL=2` – gemeinsamer Kontext-Pool für zwei gleichzeitige
  Anfragen (`--kv-unified`).
- `LLM_CTX_PER_SLOT=72000` – Obergrenze je Platz, damit der andere mindestens ~30k behält.
  `/props` meldet dann `n_ctx = 72000`; das Backend liest sein Budget daraus.
- `LLM_MTP=true`, `LLM_MTP_DRAFT=3` – spekulatives Dekodieren über den MTP-Kopf des Modells.
  Qwen3.6 trägt ihn in der Modelldatei. Bringt ein Modell ihn als eigene Datei mit (Gemma 4:
  `mtp-gemma-4-26B-A4B-it.gguf`), steht ihr Name in `LLM_MTP_MODEL_FILE`.
- `LLM_TEMPERATURE`, `LLM_TOP_P`, `LLM_TOP_K`, `PRESENCE_PENALTY` – Sampling der Antworten.
  Leer gelten die Werte für Qwen (0,6 mit Denken, 0,7 ohne; top_k 20; presence_penalty 1,5).
  Für ein anderes Modell die Werte aus seiner Modellkarte eintragen, für Gemma 4 etwa
  `1.0`, `0.95`, `64` und `0`. Kurze interne Prüfungen behalten ihre niedrige Temperatur.

Messung (RTX 5070 Ti 16 GB, DDR5-6200, n-cpu-moe 25, MTP 3):

| | ohne MTP | mit MTP |
|---|---|---|
| Ausgabe, kurzer Prompt | 56 Token/s | 79 Token/s |
| Ausgabe nach 60k Kontext | – | 70 Token/s (57 % Entwürfe angenommen) |
| Einlesen | ~2900 Token/s | ~2800 Token/s |
| zwei Anfragen gleichzeitig | – | je ~47 Token/s |

`bench.sh`, `bench2.sh` und `bench3.sh` messen das im Container (`docker cp` + `docker exec`).

## Dokumentensuche

`document_search` sucht hybrid (`rag_backend/retrieval.py`):
1. Der Container `embed` bettet die Frage mit bge-m3 ein. Unabhängig davon wird sie in
   Stichworte zerlegt, Kennungen wie `HA-40817-K` auch am Stück.
2. Qdrant liefert je 40 Kandidaten aus Vektor- und Stichwortsuche (BM25 als Sparse-Vektor, die
   IDF rechnet Qdrant) und führt beide per Reciprocal Rank Fusion zusammen.
3. Die besten 12 bewertet bge-reranker-v2-m3 im Container `rerank`. Treffer unter der
   Schwelle fallen weg, höchstens 5 gehen an das Modell.

Jeder Abschnitt trägt den Dateinamen als Kopfzeile, damit Fragen nach einem Dokument beim
Namen treffen. Fällt `embed` oder `rerank` aus, sucht der Rest weiter und das Ergebnis sagt
es dem Modell. Das Backend lädt selbst keine Modelle (kein PyTorch, kein Hugging-Face-Token).

| Schalter | Standard | Wirkung |
|---|---|---|
| `HARD_MIN_SCORE` | `0.05` | Mindestwert des Rerankers (0..1). Gemessen in `eval/retrieval`: richtige Treffer ab ~0,02, themenfremde Fragen meist darunter. |
| `RERANK_CANDIDATES` | `12` | Kandidaten für den Reranker. Bestimmt die Dauer (12: ~3,5 s auf der CPU). |
| `K_PER_METHOD` | `40` | Vorauswahl je Verfahren (Vektor, Stichworte). |
| `K_RERANKED` | `5` | Treffer, die an das Modell gehen. |

Messung vorher/nachher (60 Fragen, 30 erfundene Dokumente, `eval/retrieval/README.md`):
richtiges Dokument an erster Stelle 67 % → 96 %, Suche ohne Treffer trotz Antwort im Bestand
25 % → 2 %, Dauer je Suche 19,9 s → 3,5 s.

## Agent und Prompts

Der Systemprompt setzt sich aus drei Dateien zusammen: `SOUL.md` (Haltung, Ton),
`SKILL.md` (Werkzeuge) und `AGENTS.md` (Arbeitsweise). Die Vorgaben liegen versioniert in
`rag_backend/prompts/identity/`. Eine gleichnamige Datei in `<Arbeitsordner>/identity/` ersetzt
die Vorgabe. Abschnitte zu einem Werkzeug erscheinen nur, wenn das angemeldete Konto es hat;
`{{OWNER}}` ist die angemeldete Person. Eigene Dateien können dieselben Platzhalter benutzen
(`{{OWNER}}`, `{{ANREDE}}`, `{{#tool:NAME}}…{{/tool:NAME}}`, Beschreibung in
`rag_backend/prompts/__init__.py`).

Die Vorgaben sprechen wie ein sachlicher Kollege: freundlich, knapp, ohne Floskeln und ohne
Hinweise wie „ich darf nicht beraten“. Unterlagen (Verträge, Angebote, Richtlinien,
Datenblätter, Prüfberichte) beurteilt Argus konkret gegen das Übliche. Wo eine Fachstelle
entscheiden muss (Recht, Datenschutz, Exportkontrolle), nennt es sie einmal am Ende. Ein
eigener Abschnitt zur Vertraulichkeit verbietet interne Namen, Zahlen und Dateinamen in
Websuchen; findet Argus eine genannte Datei nicht, bittet es um den Anhang, statt im Web zu
suchen. `ARGUS_ANREDE=Sie` stellt die Anrede von „du“ (Vorgabe) auf „Sie“ um.

Unabhängig von den Dateien hängt das Backend eine Regel für interne Fragen an: Richtlinien,
Verträge, Preise und Datenblätter erst in den Dokumenten suchen, keine Zahlen selbst
einsetzen, Internes nicht im Web suchen. Die Dokumentsuche wird dem Modell immer angeboten,
auch wenn der Router auf Web-Recherche tippt. Je Frage laufen höchstens zwei Recherchen
(`RESEARCH_CALLS_PER_TURN`), je Recherche-Unteragent höchstens acht Websuchen
(`SUBAGENT_MAX_SEARCHES`).

Gemessen mit `eval/agent` (32 Aufgaben gegen den echten Agenten und das echte Modell, je 3
Runden, Details in `eval/agent/README.md`):

| Stand | Hauptaufgaben | Median je Aufgabe | Zurückgehaltene Kontrollaufgaben |
|---|---|---|---|
| Ausgang | 7 von 17 | 155 s, 4 × Zeitlimit | – |
| Router, Deckel | 61 von 78 | 16 s | 10 von 18 |
| plus Regel, Vorgaben aus dem Repo | 73 von 78 | 23 s | 15 von 18 |
| plus Firmenton und Vertraulichkeit | 73 von 78 | 24 s | 18 von 18 |

## Sicherheits-Schalter

Alle per `.env` änderbar:

| Schalter | Standard | Wirkung |
|---|---|---|
| `DASHBOARD_SETUP_TOKEN` | von `init_stack.py` erzeugt | Einrichtungs-Schlüssel für das erste Administrator-Konto. Leer = wer die Seite zuerst erreicht, wird Administrator. |
| `DASHBOARD_ALLOW_REGISTER` | `false` | Selbstregistrierung neuer Konten (immer als Benutzer). |
| `DASHBOARD_ORIGINS`, `TRUSTED_HOSTS` | `127.0.0.1`, `localhost` | Adressen, unter denen die Oberfläche aufgerufen werden darf (CSRF, DNS-Rebinding). |
| `DASHBOARD_TRACES_URL` | `http://127.0.0.1:6006` | Adresse von Phoenix für den Reiter *Traces*; nur diese Adresse darf die Seite einbetten. Leer = der Reiter zeigt nur einen Hinweis. |
| `EGRESS_AFTER_SENSITIVE` | `block` | Weg nach außen, nachdem ein Gespräch Firmendaten gelesen hat. `block` sperrt ihn. `allow` lässt ihn offen und sperrt nur Anfragen mit Bestandteilen der gelesenen Daten; fehlen die Fingerabdrücke (nach einem Neustart), wirkt auch `allow` wie `block`. |
| `WEB_INJECTION_GUARD` | `true` | Prüft Websuche und Seitenabruf auf eingeschleuste Anweisungen: der ganze Text in Abschnitten von 16.000 Zeichen; mehr als sechs Abschnitte gelten als verdächtig. |
| `TRACE_CONTENT` | `redacted` | Was Phoenix sieht: `redacted` (Inhalte mit geschwärzten Schlüsseln und PII) oder `metadata` (nur Ablauf, Dauer, Token-Zahlen, Werkzeugnamen). Einen Klartext-Modus gibt es nicht. |
| `CANARY_TOKENS` | von `init_stack.py` erzeugt | Köder-Schlüssel (Komma-getrennt, je ab 12 Zeichen). Taucht einer in einer Cloud-Anfrage, Websuche, URL oder in Werkzeug-Parametern auf, wird blockiert, als Fehler protokolliert und im Audit vermerkt. |
| `SEARXNG_SECRET` | von `init_stack.py` erzeugt | HMAC-Schlüssel für SearXNG. |
| `WEBPAGE_MAX_BYTES` | `3000000` | Höchstens so viele Bytes liest der Seitenabruf; meldet eine Seite vorab mehr, wird sie gar nicht geladen. |
| `AUDIT_SIEM` | `off` | Weitergabe jedes Audit-Eintrags: `syslog` an einen Syslog-Empfänger (RFC 5424), `stdout` als JSON-Zeile ins Container-Log (für einen Log-Sammler). |
| `AUDIT_SIEM_FORMAT` | `json` | `json` oder `cef` (Common Event Format, etwa für Microsoft Sentinel, Falcon LogScale, QRadar). |
| `AUDIT_SYSLOG_HOST`, `AUDIT_SYSLOG_PORT`, `AUDIT_SYSLOG_PROTOCOL` | –, `514`, `udp` | Ziel für `syslog`. Bei `tcp` mit Längenangabe je Nachricht (RFC 6587), die Verbindung wird gehalten und nach Fehlern neu aufgebaut. |
| `AUDIT_SIEM_HOSTNAME` | Name des Containers | Rechnername im Syslog-Kopf und im Feld `host`. |
| `AUDIT_TRUST_FORWARDED` | `false` | Schreibt die erste Adresse aus `X-Forwarded-For` mit ins Protokoll. Nur hinter einem eigenen Proxy einschalten, sonst ist der Wert gefälscht. |

Die Weitergabe ans SIEM blockiert keine Anfrage: Einträge gehen über eine Warteschlange
(10.000 Plätze). Ist das SIEM nicht erreichbar oder die Warteschlange voll, wird der Eintrag
nicht nachgeliefert, sondern gezählt und höchstens einmal je Minute gewarnt. Maßgeblich
bleibt das Protokoll in der Datenbank. Jeder weitergegebene Eintrag trägt seine
HMAC-Signatur, so lässt er sich dort wiederfinden.
| `CLOUD_AGENTS_ENABLED` | `false` | Cloud-Modelle (Gemini) als Helfer. |
| `CLOUD_MISSION_AUTO_APPROVE` | `false` | Mit `true` gehen Cloud-Anfragen ohne Rückfrage raus; Schwärzung, PII-Stopp und Köder-Prüfung laufen trotzdem. In einem Gespräch mit Firmendaten nie ohne Rückfrage. |
| `API_DOCS` | `false` | Mit `true` ist die API-Beschreibung unter `/docs` erreichbar, ohne Anmeldung. |
| `AUDIT_ANCHOR_PATH` | `/app/data/audit_anchor.json` | Anker der Audit-Kette: Löschen am Ende fällt bei der Prüfung auf. |

Nach fünf Fehlversuchen ist ein Konto 15 Minuten gesperrt. `alembic/` ist schreibgeschützt
eingebunden: Die Migrationen laufen beim Start als Code.

Änderungen am Backend oder an der Oberfläche brauchen einen Neubau:
`docker compose up -d --build rag-backend`.

## Geheimnisse

`API_Tokens/`, `.env` und Stimmaufnahmen in `voice_ref/` sind per `.gitignore` ausgeschlossen.
Im Repo liegt nur `.env.example` (gut 50 Einstellungen; alles andere nimmt den Standard aus dem
Code). Werte mit `ENC:` in `API_Tokens/` sind mit `fernet_key.txt` verschlüsselt; an diesem
Schlüssel hängen auch die gespeicherten Chats. Ältere Installationen haben den Modell-Schlüssel
noch als `sglang_api_key.txt`; `init_stack.py` benennt die Datei beim nächsten Lauf um.

## Tests und Messungen

```powershell
uv pip install -r rag_backend/pyproject.toml
python -m pytest rag_backend/tests -q
```

Die Tests brauchen keine laufenden Container (SQLite, Test-Schlüssel). Sie laufen in einem
Ordner ohne `.env`, etwa in einem frischen Klon: Eine vorhandene `.env` überschreibt die
Testwerte. Lokal überspringen sie vier Tests, die nur im CI laufen.

Die Tests der Sandbox laufen in ihrem eigenen Abbild, denn nur dort räumt sie zwischen zwei
Läufen auf (Prozesse beenden, `/tmp` leeren):

```powershell
docker build -t argus-sandbox-test -f calc_sandbox/Dockerfile.sandbox calc_sandbox
docker run --rm --user root -v "${PWD}/calc_sandbox:/src" -w /src --entrypoint sh argus-sandbox-test -c "pip install -q pytest httpx && exec python -m pytest test_app.py -q"
```

Im CI laufen beide bei jedem Push auf `main`, dazu die Prüfung der Compose-Dateien, der
Netz-Abschottung und des Installers (ShellCheck). Die Messungen in `eval/` laufen gegen den echten Stapel und das
echte Modell.

## Lizenz

Argus steht unter der [Apache-Lizenz 2.0](LICENSE); der Urheberhinweis steht in
[NOTICE](NOTICE).

Argus baut auf Software, Schriften und Modellen anderer auf.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) führt sie mit ihren Lizenzen auf, getrennt
danach, ob sie im Repo liegen, beim Bauen in die Abbilder kommen oder als eigener Container
laufen. Das Wichtigste für einen Einsatz im Unternehmen:

| Baustein | Lizenz | Was das heißt |
|---|---|---|
| Eigener Code von Argus | Apache 2.0 | Nutzen, ändern und weitergeben erlaubt, auch kommerziell; Lizenz und NOTICE müssen dabeibleiben. |
| Python-Pakete des Backends | MIT, BSD, Apache 2.0, LGPL (psycopg2) | Kein Paket unter GPL oder AGPL. |
| SearXNG (Websuche) | AGPL-3.0 | Läuft unverändert als eigener Container. Wer es ändert und anderen übers Netz anbietet, muss den geänderten Quelltext anbieten. |
| Arize Phoenix (Ablaufverfolgung) | Elastic License 2.0 | Im eigenen Haus nutzbar. Nicht erlaubt: Dritten als gehosteten Dienst anbieten. Wer Argus Kunden als Dienst bereitstellt, lässt Phoenix weg oder ersetzt es. |
| FFmpeg in den Sprachdiensten | GPL (Debian-Paket) | Läuft als eigenes Programm. Wer die Abbilder weitergibt, gibt FFmpeg unter der GPL weiter. |
| Modelle | je nach Modellkarte | Vor dem Einsatz die Lizenz des gewählten Sprachmodells prüfen. |

Das ist die Einschätzung des Autors, keine Rechtsberatung.
