"""Scan-Messung (eval/scans): Bewertung und Musterloesungen, ohne Modell."""
import sys
from pathlib import Path

import pytest

SCANS = Path(__file__).resolve().parents[2] / "eval" / "scans"
sys.path.insert(0, str(SCANS))

import pages  # noqa: E402
import score  # noqa: E402


def test_text_ignores_formatting_but_not_digits():
    abschrift = "**Rechnungs-Nr.:** BW‑2026‑11473\nIBAN DE44480501610043221790"
    assert score.pruefe(abschrift, {"art": "text", "soll": "BW-2026-11473"})
    assert score.pruefe(abschrift, {"art": "text", "soll": "DE44 4805 0161 0043 2217 90"})
    assert not score.pruefe(abschrift, {"art": "text", "soll": "BW-2026-11474"})
    assert score.pruefe("⌀25 h6", {"art": "text", "soll": "Ø25 h6"})
    assert score.pruefe("19.11.2026", {"art": "text", "soll": ["19. NOV. 2026", "19.11.2026"]})


def test_value_must_be_in_the_right_table_row():
    tabelle = "| M-02 | Bohrung | 35,031 | n.i.O. |\n|---|---|---|---|\n| M-07 | Länge | 62,071 | i.O. |"
    assert score.pruefe(tabelle, {"art": "zeile", "soll": ["M-02", "35,031", "n.i.O."]})
    assert not score.pruefe(tabelle, {"art": "zeile", "soll": ["M-07", "35,031"]})
    #--- "ohne": eine i.O.-Zeile, die als n.i.O. gelesen wurde, zaehlt nicht.
    assert score.pruefe(tabelle, {"art": "zeile", "soll": ["M-07", "62,071"], "ohne": ["n.i.O."]})
    assert not score.pruefe(tabelle, {"art": "zeile", "soll": ["M-02", "35,031"], "ohne": ["n.i.O."]})


def test_reading_order_and_checkboxes():
    text = "Neue Prüfanlage ... Betriebsversammlung ... Parkplatz Nord"
    assert score.pruefe(text, {"art": "reihenfolge", "soll": ["Neue Prüfanlage", "Parkplatz Nord"]})
    assert not score.pruefe(text, {"art": "reihenfolge", "soll": ["Parkplatz Nord", "Neue Prüfanlage"]})
    pruefung = {"art": "angekreuzt", "soll": "Ware in Ordnung", "nicht": ["Ware gesperrt"]}
    assert score.pruefe("[x] Ware in Ordnung\n[ ] Ware gesperrt", pruefung)
    assert score.pruefe("☒ Ware in Ordnung\n☐ Ware gesperrt", pruefung)
    assert not score.pruefe("[ ] Ware in Ordnung\n[x] Ware gesperrt", pruefung)
    assert not score.pruefe("Ware in Ordnung\nWare gesperrt", pruefung)


def test_character_error_rate():
    assert score.zeichenfehler("Rechnung 4711", "Rechnung 4711") == 0
    assert score.zeichenfehler("abcd", "abed") == 0.25


def test_pages_build_without_fonts_and_drop_handwriting():
    seiten, hinweise, _ = pages.build_pages(None)
    namen = {s.name for s in seiten}
    assert "01_rechnung_sauber" in namen and "11_foto_ohne_text" in namen
    assert "05_handschrift_notiz" not in namen and len(hinweise) == 2
    for s in seiten:
        assert pages.als_datei(s)[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xdb")


def test_stress_series_build_with_clean_unique_names():
    seiten, hinweise, _ = pages.build_pages(None, reihen=True)
    namen = [s.name for s in seiten]
    assert len(namen) == len(set(namen)) and len(hinweise) == 3       # Handschrift entfaellt
    assert all(n.isascii() and n.replace("_", "").isalnum() for n in namen)
    assert {s.reihe for s in seiten} >= {"Auflösung", "Unschärfe", "JPEG", "Drehung", "Schriftgröße"}
    #--- Geringere Aufloesung heisst: kleineres Bild, wie vom Scanner.
    groesse = {s.stufe: s.bild.size for s in seiten if s.reihe == "Auflösung"}
    assert groesse["60 dpi"][0] < groesse["150 dpi"][0] < pages.W


@pytest.mark.parametrize("seite",[s for s in pages.build_pages(None)[0] if s.zeichen_fehler],
                         ids=lambda s: s.name)
def test_ground_truth_of_prose_pages_passes_its_own_checks(seite):
    #--- Die Musterloesung muss ihre eigenen Pruefungen bestehen, sonst misst die
    #--- Messung einen Fehler im Bestand statt im Modell.
    for p in seite.pruefungen:
        assert score.pruefe(seite.text, p), score.beschreibe(p)
    assert score.zeichenfehler(seite.text, seite.text) == 0


def test_ground_truth_of_the_test_report_table():
    zeilen = ["| " + " | ".join(z) + " |" for z in pages._PRUEF]
    abschrift = ("Prüfprotokoll Erstmuster EMPB-2026-118\nCharge 2611-07\n" + "\n".join(zeilen)
                 + "\nErgebnis: 2 Merkmale n.i.O. – Teil gesperrt")
    seite = next(s for s in pages.build_pages(None)[0] if s.name.startswith("04_"))
    for p in seite.pruefungen:
        assert score.pruefe(abschrift, p), score.beschreibe(p)
    #--- Still verfaelschte Ziffern und eine falsche Bewertung fallen auf (beobachtet bei
    #--- 60 dpi: 0,62 -> 0,82 und M8 -> M1).
    def verfehlt(text):
        return [score.beschreibe(p) for p in seite.pruefungen if not score.pruefe(text, p)]

    assert len(verfehlt(abschrift.replace("| 0,62 |", "| 0,82 |"))) == 1
    assert len(verfehlt(abschrift.replace("Gewinde M8x1,25", "Gewinde M1x1,25"))) == 1
    assert len(verfehlt(abschrift.replace("| 84,006 | i.O. |", "| 84,006 | n.i.O. |"))) == 1
