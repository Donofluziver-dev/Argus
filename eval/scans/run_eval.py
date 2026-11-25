"""Messung: Wie sicher liest das lokale Modell Scans, Fotos und Handschrift?

Erzeugt die Testseiten aus pages.py, schickt jede ueber denselben Weg wie die Aufnahme in
die Dokumentensuche an das Modell (rag_backend.vision.transcribe_image: verkleinern auf
VISION_MAX_SIDE, Abschreib-Anweisung, Temperatur 0) und bewertet mit score.py.

Laeuft im Backend-Abbild im internen Netz des Stapels, der Modell-Dienst muss mit
Bild-Encoder laufen (LLM_MMPROJ_FILE). Unter Windows (Projektname argus-code):

  docker run --rm --network argus-code_intern \
    -v <Repo>/rag_backend:/app/rag_backend:ro \
    -v <Repo>/eval:/app/eval:ro \
    -v <Repo>/API_Tokens/llm_api_key.txt:/run/secrets/llm_api_key:ro \
    -v <Repo>/API_Tokens/fernet_key.txt:/app/fernet_key.txt:ro \
    -v C:/Windows/Fonts:/fonts:ro \
    --entrypoint python argus/rag-backend:local /app/eval/scans/run_eval.py --label standard

Das Netz heisst <Projektname>_intern (docker network ls). Unter Linux statt der
Windows-Schriften etwa /usr/share/fonts einhaengen; ohne Handschrift-Schrift entfallen die
Seiten mit Handschrift.

  --reihen          statt der elf Seiten die Belastungsreihen: dieselbe Pruefungstabelle bei
                    sinkender Aufloesung, mehr Unschaerfe, staerkerer Kompression, blasserer
                    und kleinerer Schrift, gedreht; dazu schwierigere Handschriften
  --set NAME=WERT   Umgebungsvariable vor dem Import, z. B. --set VISION_MAX_SIDE=2200
  --nur NAME        nur Seiten, deren Name NAME enthaelt (mehrfach moeglich)
  --ablage DIR      legt Seitenbilder und Abschriften dort ab (zum Nachsehen)
  --abschriften DIR bewertet die Abschriften aus einer frueheren --ablage neu, ohne Modell
                    (nach einer Aenderung an den Pruefungen)
  --json            gibt zusaetzlich jede Seite als JSON aus
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pages  # noqa: E402
import score  # noqa: E402


def run(label: str, font_dir: str, nur: list[str], ablage: Path | None,
        reihen: bool = False, abschriften: Path | None = None) -> tuple[list[dict], dict]:
    seiten, hinweise, schriften = pages.build_pages(font_dir, reihen=reihen)
    if nur:
        seiten = [s for s in seiten if any(n in s.name for n in nur)]
    if ablage:
        ablage.mkdir(parents=True, exist_ok=True)

    rows = []
    for s in seiten:
        daten = pages.als_datei(s)
        t0 = time.monotonic()
        fehler = None
        if abschriften:
            datei = abschriften / f"{s.name}.txt"
            text = datei.read_text(encoding="utf-8") if datei.exists() else ""
            fehler = None if datei.exists() else "keine Abschrift"
        else:
            from rag_backend import vision
            try:
                text = vision.transcribe_image(daten)
            except Exception as e:
                text, fehler = "", f"{type(e).__name__}: {e}"
        dauer = time.monotonic() - t0
        ergebnisse = [(p, score.pruefe(text, p)) for p in s.pruefungen]
        gruppen = {}
        for p, ok in ergebnisse:
            if p.get("gruppe"):
                g = gruppen.setdefault(p["gruppe"], [0, 0])
                g[0] += ok
                g[1] += 1
        row = {
            "seite": s.name, "titel": s.titel, "reihe": s.reihe, "stufe": s.stufe,
            "sekunden": round(dauer, 1),
            "gruppen": {g: f"{a}/{b}" for g, (a, b) in gruppen.items()},
            "treffer": sum(1 for _, ok in ergebnisse if ok), "pruefungen": len(ergebnisse),
            "verfehlt": [score.beschreibe(p) for p, ok in ergebnisse if not ok],
            "cer": round(score.zeichenfehler(s.text, text), 3) if s.zeichen_fehler and text else None,
            "fehler": fehler, "zeichen": len(text),
        }
        rows.append(row)
        if ablage:
            endung = "jpg" if s.format == "JPEG" else "png"
            (ablage / f"{s.name}.{endung}").write_bytes(daten)
            (ablage / f"{s.name}.txt").write_text(text, encoding="utf-8")
        print(f"  {s.name:<28} {row['treffer']:>2}/{row['pruefungen']:<2} "
              f"{dauer:5.1f} s" + (f"  CER {row['cer']:.1%}" if row["cer"] is not None else "")
              + "".join(f"  {g} {v}" for g, v in row["gruppen"].items())
              + (f"  FEHLER {fehler}" if fehler else ""), flush=True)

    treffer = sum(r["treffer"] for r in rows)
    gesamt = sum(r["pruefungen"] for r in rows)
    cers = [r["cer"] for r in rows if r["cer"] is not None]
    summary = {
        "label": label,
        "vision_max_side": os.getenv("VISION_MAX_SIDE", "1600"),
        "seiten": len(rows),
        "pruefungen": gesamt,
        "treffer": treffer,
        "treffer_anteil": round(treffer / gesamt, 3) if gesamt else None,
        "seiten_fehlerfrei": sum(1 for r in rows if r["treffer"] == r["pruefungen"]),
        "cer_median": round(statistics.median(cers), 3) if cers else None,
        "sekunden_median": round(statistics.median([r["sekunden"] for r in rows]), 1) if rows else None,
        "sekunden_summe": round(sum(r["sekunden"] for r in rows), 1),
        "schriften": schriften,
        "hinweise": hinweise,
    }
    if reihen:
        summary["reihen"] = {}
        for r in rows:
            teile = ", ".join(f"{g} {v}" for g, v in r["gruppen"].items())
            summary["reihen"].setdefault(r["reihe"], []).append(
                f"{r['stufe']}: {r['treffer']}/{r['pruefungen']}" + (f" ({teile})" if teile else ""))
    return rows, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=WERT")
    ap.add_argument("--fonts", default="/fonts")
    ap.add_argument("--nur", action="append", default=[])
    ap.add_argument("--ablage", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reihen", action="store_true")
    ap.add_argument("--abschriften", type=Path)
    args = ap.parse_args()

    for item in args.set:
        k, _, v = item.partition("=")
        os.environ[k] = v
    os.environ.setdefault("OAI_BASE_URL", "http://llm:8080/v1")

    rows, summary = run(args.label, args.fonts, args.nur, args.ablage, reihen=args.reihen,
                        abschriften=args.abschriften)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in rows:
        for v in r["verfehlt"]:
            print(f"  verfehlt {r['seite']}: {v}")


if __name__ == "__main__":
    main()
