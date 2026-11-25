"""Bewertung der Scan-Messung: Was das Modell abgeschrieben hat, gegen die Musterloesung.

Pruefungen (in pages.py je Seite):
  text         soll (oder eine von mehreren Schreibweisen) steht irgendwo im Text
  zeile        alle Teile stehen in derselben Zeile (Tabellen: Wert gehoert zur richtigen Zeile);
               mit ohne darf die Zeile diese Teile nicht enthalten (i.O. statt n.i.O.)
  reihenfolge  die Teile stehen in dieser Reihenfolge (Lesereihenfolge, etwa bei Spalten)
  angekreuzt   die Zeile mit soll traegt ein Kreuz, die Zeilen in nicht tragen keins
  beginnt      der Text beginnt mit soll
  mindestlaenge  der Text hat mindestens soll Zeichen (nicht durch eine Anweisung im Bild ersetzt)

Verglichen wird tolerant gegen Formatierung: Gross/klein, Markdown-Zeichen, Bindestrich-
Varianten und Leerzeichen zaehlen nicht. Ziffern, Kommas und Punkte zaehlen.
Die Zeichenfehlerrate (CER) ist der Editierabstand zum Referenztext, geteilt durch dessen
Laenge, nach derselben Normalisierung. Sie gibt es nur fuer Seiten mit Fliesstext.
"""
from __future__ import annotations

import re
import unicodedata

_STRICHE = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_MARKDOWN = re.compile(r"[|*#_`>]+")
_TABELLENLINIE = re.compile(r"^\s*:?-{3,}:?\s*$")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_STRICHE)
    text = text.replace("⌀", "ø").replace("×", "x").replace(" ", " ")
    text = _MARKDOWN.sub(" ", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _ohne_leer(text: str) -> str:
    return text.replace(" ", "")


def _enthaelt(heu: str, nadel: str) -> bool:
    n, h = norm(nadel), heu
    return n in h or _ohne_leer(n) in _ohne_leer(h)


def _zeilen(text: str) -> list[str]:
    out = []
    for z in (text or "").splitlines():
        if _TABELLENLINIE.match(z.replace("|", "")):
            continue
        z = norm(z)
        if z:
            out.append(z)
    return out


#--- Ein Kreuz, wie es ein Modell schreibt: [x], (x), ein Zeichen wie ☒ oder ein x allein
#--- am Anfang oder Ende der Zeile. "max" oder "M8x1" zaehlen nicht (kein Wortanfang).
_KREUZ = re.compile(r"(\[\s*x\s*\]|\(\s*x\s*\)|[☒☑✓✔✗✘☓⊠]|^x\b|\bx$|\bangekreuzt\b)")


def _hat_kreuz(zeile: str) -> bool:
    return bool(_KREUZ.search(zeile))


def pruefe(text: str, pruefung: dict) -> bool:
    art, soll = pruefung["art"], pruefung["soll"]
    gesamt = norm(text)
    if art == "text":
        varianten = soll if isinstance(soll, list) else [soll]
        return any(_enthaelt(gesamt, v) for v in varianten)
    if art == "zeile":
        ohne = [norm(o) for o in pruefung.get("ohne", [])]
        return any(all(_enthaelt(z, teil) for teil in soll) and not any(o in z for o in ohne)
                   for z in _zeilen(text))
    if art == "reihenfolge":
        pos = 0
        for teil in soll:
            i = gesamt.find(norm(teil), pos)
            if i < 0:
                return False
            pos = i + 1
        return True
    if art == "angekreuzt":
        zeilen = _zeilen(text)
        ja = [z for z in zeilen if norm(soll) in z]
        if not ja or not any(_hat_kreuz(z) for z in ja):
            return False
        for anderes in pruefung.get("nicht", []):
            for z in zeilen:
                if norm(anderes) in z and norm(soll) not in z and _hat_kreuz(z):
                    return False
        return True
    if art == "beginnt":
        return gesamt.startswith(norm(soll))
    if art == "mindestlaenge":
        return len(gesamt) >= int(soll)
    raise ValueError(f"unbekannte Pruefung: {art}")


def beschreibe(pruefung: dict) -> str:
    soll = pruefung["soll"]
    if isinstance(soll, list):
        soll = " / ".join(map(str, soll)) if pruefung["art"] == "text" else " + ".join(map(str, soll))
    return f"{pruefung['art']}: {soll}"


def editierabstand(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    vorher = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        jetzt = [i]
        for j, cb in enumerate(b, 1):
            jetzt.append(min(vorher[j] + 1, jetzt[j - 1] + 1, vorher[j - 1] + (ca != cb)))
        vorher = jetzt
    return vorher[-1]


def zeichenfehler(referenz: str, text: str) -> float:
    r, t = norm(referenz), norm(text)
    return editierabstand(r, t) / max(1, len(r))
