# RAG Retrieval-Evaluation: Halvorsen Antriebstechnik GmbH

Dieser Testbestand dient dem quantitativen und qualitativen Benchmark von Dokumentensuch- und Retrieval-Verfahren (insbesondere vor und nach Einführung einer Hybridsuche aus dichter Vektorsuche, lexikalischer BM25-Suche und neuronalem Cross-Encoder-Reranking).

> **Wichtiger Hinweis (Fiktive Daten):** Sämtliche in diesem Datensatz enthaltenen Unternehmen, Personen, Telefonnummern (im Format `0521 555-xxxx`), Verträge, Kennungen, Beträge und technischen Spezifikationen sind frei erfunden. Ähnlichkeiten mit real existierenden Firmen, Produkten oder lebenden Personen sind rein zufällig.

---

## 1. Verzeichnis- und Dateistruktur

```
eval/retrieval/
├── README.md               # Diese Dokumentation
├── queries.json            # 60 Testfragen inklusive Referenztreffern und Fragetypen
├── run_eval.py             # Messprogramm (Aufruf im Kopf der Datei)
└── corpus/                 # Korpus mit genau 30 Markdown-Dateien (UTF-8)
    ├── reisekostenrichtlinie_2024.md
    ├── reisekostenrichtlinie_2025.md
    ├── rahmenvertrag_kessler_logistik_2025.md
    ├── rahmenvertrag_vogel_transporte_2025.md
    ├── produktdatenblatt_torqdrive_xd500.md
    ├── produktdatenblatt_torqdrive_xd700.md
    ├── stueckliste_planetenstufe_ha_40817_k.md
    ├── stueckliste_planetenstufe_ha_40871_k.md
    ├── reklamation_charge_rk_2025_0041.md
    ├── reklamation_charge_rk_2025_0042.md
    ├── it_sicherheitsrichtlinie.md                      # Langdokument (~18,3k Zeichen)
    ├── wartungsanleitung_planetengetriebe_pg3.md        # Langdokument (~12,0k Zeichen)
    ├── urlaubs_und_homeoffice_regelung.md               # Langdokument (~12,5k Zeichen)
    ├── supplier_evaluation_global_bearings_2025.md      # Englisch
    ├── project_status_report_hydradrive_q1_2025.md      # Englisch
    ├── technical_datasheet_vfd_inverter_iv900.md        # Englisch
    ├── preisliste_standardantriebe_2025.md
    ├── onboarding_checkliste_technischer_vertrieb.md
    ├── besprechungsprotokoll_entwicklungsmeeting_juni_2025.md
    ├── besprechungsprotokoll_werksleitung_linz_maerz_2025.md
    ├── projektstatus_elektromobilitaet_ecodrive_2025.md
    ├── lieferantenbewertung_metallguss_saxonia_2024.md
    ├── rechnungsuebersicht_fertigungsmaterial_q1_2025.md
    ├── internes_faq_betriebliche_altersvorsorge.md
    ├── stueckliste_flanschmotor_ha_22091_k.md
    ├── produktdatenblatt_kegelradgetriebe_kg200.md
    ├── wartungsanleitung_schmierstoffwechsel_allgemein.md
    ├── pruefprotokoll_endabnahme_chargenpruefung_cp25.md
    ├── brandschutz_und_evakuierungsordnung_2025.md
    └── rahmenvertrag_energieversorgung_stadtwerke_2025.md
```

---

## 2. Zusammensetzung des Korpus

Der Korpus umfasst **genau 30 Markdown-Dokumente** mit industrieüblichen Dokumententypen des Maschinenbaus:
- **Rahmenverträge:** Format `HAT-V-2025-0xxx` (Logistik, Spedition, Energieversorgung)
- **Stücklisten / BOM:** Format `HA-12345-K` (Planetenstufen, Elektromotoren)
- **Reiseregelungen:** Reisekostenrichtlinie 2024 und 2025 im identischen Aufbau mit differierenden Sätzen
- **Betriebsvereinbarungen:** Urlaub, Gleitzeit, Ergonomie und Homeoffice
- **Compliance & Sicherheit:** Konzernweite IT-Sicherheitsrichtlinie, Brandschutz- und Evakuierungsordnung
- **Besprechungsprotokolle:** Entwicklungs-Reviews und Werksleitungssitzungen
- **Technische Dokumentation:** Produktdatenblätter (Servomotoren, Kegelradgetriebe), Wartungsanweisungen (Spülung, Montage)
- **Qualitätswesen:** 8D-Reklamationsberichte (`RK-2025-xxxx`), Lieferantenbewertungen, Prüfprotokolle (`CP-2025-091`)
- **HR & Finanzen:** Internes FAQ zur Altersvorsorge, Onboarding-Checkliste, Rechnungsprüfungsübersichten (`RE-2025-xxxx`)

### Dokumentenlängen und Chunking-Herausforderungen
- **Standardlänge:** 27 Dokumente weisen eine Länge zwischen 1.500 und 6.000 Zeichen auf.
- **Langdokumente (12.000 bis 18.300 Zeichen):** Genau 3 Dokumente (`it_sicherheitsrichtlinie.md`, `wartungsanleitung_planetengetriebe_pg3.md`, `urlaubs_und_homeoffice_regelung.md`) übersteigen typische Chunk-Größen. Die abgefragten Zielinformationen liegen hier gezielt in der zweiten Dokumentenhälfte (Abschnitte 7–10), um die Relevanzhaltung über Chunk-Grenzen hinweg zu testen.
- **Sprachen:** 27 Dokumente in deutscher Sprache, genau 3 Dokumente in englischer Sprache (`supplier_evaluation_global_bearings_2025.md`, `project_status_report_hydradrive_q1_2025.md`, `technical_datasheet_vfd_inverter_iv900.md`).

### Harte Ablenker-Paare (Distraktoren)
Um zu prüfen, ob die Suche bei minimalen Ziffern- oder Begriffsänderungen das korrekte Dokument priorisiert, enthält der Bestand fünf gezielte Ablenker-Paare:
1. **Reisekosten 2024 vs. 2025:** `reisekostenrichtlinie_2024.md` vs. `reisekostenrichtlinie_2025.md` (identische Gliederung, andere Kilometersätze, Hotelgrenzen und Bahnregeln).
2. **Rahmenverträge Spedition:** `rahmenvertrag_kessler_logistik_2025.md` (`HAT-V-2025-0104`) vs. `rahmenvertrag_vogel_transporte_2025.md` (`HAT-V-2025-0109`) (identischer Aufbau, andere Standorte, Skonti und Haftungslimits).
3. **Produktdatenblätter derselben Modellreihe:** `produktdatenblatt_torqdrive_xd500.md` vs. `produktdatenblatt_torqdrive_xd700.md` (gleiche Produktlinie, unterschiedliche Drehmomente: 500 Nm vs. 720 Nm, Schutzart IP65 vs. IP67).
4. **Stücklisten mit Zahlendreher in der Teilenummer:** `stueckliste_planetenstufe_ha_40817_k.md` (`HA-40817-K`) vs. `stueckliste_planetenstufe_ha_40871_k.md` (`HA-40871-K`) (Standardausführung vs. verstärkte Heavy-Duty-Stufe).
5. **Reklamationsberichte mit fortlaufender Ticketnummer:** `reklamation_charge_rk_2025_0041.md` (`RK-2025-0041`) vs. `reklamation_charge_rk_2025_0042.md` (`RK-2025-0042`) (Härtefehler Antriebswelle vs. Flankenspielfehler Kegelrad).

---

## 3. Fragetypen in `queries.json`

Die Datei `queries.json` enthält ein JSON-Array mit **genau 60 Testabfragen**, aufgeteilt in 7 standardisierte Kategorien:

| Typ | Anzahl | Beschreibung und Bewertungsziel |
| :--- | :--- | :--- |
| `semantic` | 15 | Alltagsnahe Umschreibungen mit Synonymen. Enthält keine 3-Wort-Folge aus dem Quelldokument. Testet die Leistungsfähigkeit dichter Vektormodelle gegenüber klassischer Stichwortsuche. |
| `exact_id` | 12 | Gezielte Abfragen nach Teile-, Vertrags-, Ticket- oder Rechnungsnummern mit minimalem Kontext. Mindestens 4 Abfragen zielen auf die harten Ablenker ab, um BM25- und Exact-Match-Stärken zu fordern. |
| `name` | 8 | Fragen nach Personen oder Firmen, die jeweils in nur genau einem Dokument vorkommen (z. B. Chioma Okonkwo, Bernd Krawinkel, Alistair Vance). |
| `mixed` | 8 | Kombination aus semantischer Umschreibung und konkreter Kennung bzw. Personennamen. Testet das Zusammenspiel in Hybridsuch-Pipelines. |
| `english` | 5 | Englisch formulierte Fragestellungen, die sich auf deutschsprachige Dokumente beziehen (Cross-Lingual Retrieval). |
| `filename` | 4 | Fragen, in denen der Anwender den Dokumententitel explizit als Referenz nennt. |
| `no_answer` | 8 | Plausible Fragestellungen ohne Antwort im Korpus (`"relevant": []`, `"answer_hint": ""`). Davon 3 Beinahe-Treffer (nicht existente Teilenummer `HA-40819-K`, fehlendes Jahr 2023, unbekannte Person Dr. Bergmann) und 5 themenfremde Betriebsfragen (Kantine, Parkplatz, JobRad, Kita, Fitness). Dient zur Messung von False-Positive-Raten und Reranker-Schwellenwerten. |

---

## 4. Messung

`run_eval.py` indexiert den Korpus in eine Wegwerf-Sammlung in Qdrant, stellt jede Frage über
das echte `document_search` des Backends und löscht die Sammlung danach. Gemessen wird also
derselbe Weg wie im Chat: Zerlegen, Einbetten, Vorauswahl, Reranker, Schwelle. Der Aufruf
steht im Kopf der Datei.

- **hit@1 / hit@5:** Anteil der beantwortbaren Fragen, bei denen ein relevantes Dokument an
  erster Stelle bzw. unter den ersten fünf steht.
- **MRR:** Mittel von 1/Rang des ersten relevanten Dokuments.
- **leer:** Anteil der beantwortbaren Fragen ohne jeden Treffer (alles unter der Schwelle).
- **ohne Antwort korrekt leer:** Anteil der `no_answer`-Fragen, bei denen nichts zurückkommt.

## 5. Ergebnisse (10.11.2026)

Vorher: bge-m3 und bge-reranker-v2-m3 über PyTorch im Backend, nur Vektorsuche, 8 Kandidaten,
Schwelle 0,55. Nachher: Hybridsuche (bge-m3 im Container `embed` plus BM25 in Qdrant, RRF),
12 Kandidaten für bge-reranker-v2-m3 im Container `rerank` (8 Threads), Schwelle 0,05.

| | vorher | nachher |
|---|---|---|
| hit@1 (52 beantwortbare Fragen) | 67 % | 96 % |
| hit@5 | 67 % | 98 % |
| beantwortbar, aber leer | 25 % | 2 % |
| ohne Antwort korrekt leer (8) | 6 | 5 |
| Dauer je Suche, Median | 19,9 s | 3,5 s |
| 30 Dokumente einlesen | 232 s | 35 s |

Nach Typ vorher → nachher (hit@1): semantic 47 → 93 %, exact_id 92 → 100 %, name 75 → 100 %,
mixed 63 → 100 %, english 60 → 80 %, filename 75 → 100 %.

Schwelle: Richtige Treffer lagen beim Reranker bis hinunter zu 0,02. Themenfremde Fragen lagen
meist darunter, Beinahe-Treffer (fast gleiche Teilenummer, Richtlinie eines anderen Jahres)
bei 0,14 bis 0,39. 0,05 verwirft die themenfremden und behält 98 % der richtigen. Die
Beinahe-Treffer kommen mit niedrigem Wert zurück; der Agent sieht Wert und Inhalt. Die alte
Schwelle 0,55 hätte auch mit der Hybridsuche ein Viertel der richtigen Treffer verworfen.

24 statt 12 Kandidaten brachten im Bestand keinen Treffer mehr, kosteten aber die doppelte
Zeit. Die Dauer vorher war durch `OMP_NUM_THREADS` aus der `.env` auf wenige Kerne begrenzt,
so lief es auch im Betrieb.

Der Bestand ist klein und die Schwelle an ihm gewählt. Eine Messung mit echten Firmendokumenten
und echten Fragen gehört vor jeden Einsatz.
