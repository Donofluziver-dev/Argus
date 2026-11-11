"""Messung der Dokumentensuche.

Indexiert eval/retrieval/corpus in eine Wegwerf-Sammlung, stellt die Fragen aus
queries.json ueber das echte document_search und zaehlt, ob das richtige
Dokument vorn liegt. Gemessen wird also derselbe Weg wie im Chat: Zerlegen,
Einbetten, Suchen, Reranker, Schwelle.

Laeuft im Backend-Abbild im internen Netz des Stapels (Qdrant, embed, rerank):

  docker run --rm --network argus_intern \
    -v <Repo>/rag_backend:/app/rag_backend:ro \
    -v <Repo>/eval:/app/eval:ro \
    -v <Repo>/API_Tokens/llm_api_key.txt:/run/secrets/llm_api_key:ro \
    -v <Repo>/API_Tokens/fernet_key.txt:/app/fernet_key.txt:ro \
    --entrypoint python argus-rag-backend /app/eval/retrieval/run_eval.py --label nachher

"argus" ist der Name des Compose-Projekts. Wer in der .env COMPOSE_PROJECT_NAME
gesetzt hat, nimmt diesen Namen fuer Netz (<Name>_intern) und Abbild
(<Name>-rag-backend).

  --set NAME=WERT   setzt Umgebungsvariablen vor dem Import (mehrfach moeglich),
                    z. B. --set HARD_MIN_SCORE=0 --set K_RERANKED=10 fuer die
                    Verteilung aller Reranker-Werte.
  --json            gibt zusaetzlich jede Frage als JSON aus.
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
_DOC_RE = re.compile(r'<document index="\d+" source="(?P<src>[^"]*?)(?: \(Chunk \d+\))?"[^>]*relevance="(?P<score>-?[\d.]+)"')


def parse_hits(output: str) -> list[tuple[str, float]]:
    """Dokumente in Ausgabe-Reihenfolge, je Dokument nur der erste Abschnitt."""
    seen, hits = set(), []
    for m in _DOC_RE.finditer(output or ""):
        src = m.group("src")
        if src not in seen:
            seen.add(src)
            hits.append((src, float(m.group("score"))))
    return hits


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["relevant"]]
    no_answer = [r for r in rows if not r["relevant"]]

    def block(rs):
        if not rs:
            return {}
        ranks = [r["rank"] for r in rs]
        return {
            "n": len(rs),
            "hit@1": sum(1 for x in ranks if x == 1) / len(rs),
            "hit@5": sum(1 for x in ranks if x is not None and x <= 5) / len(rs),
            "mrr": sum(1 / x for x in ranks if x) / len(rs),
            "leer": sum(1 for r in rs if not r["hits"]) / len(rs),
        }

    by_type = {}
    for t in sorted({r["type"] for r in answerable}):
        by_type[t] = block([r for r in answerable if r["type"] == t])
    lat = [r["seconds"] for r in rows]
    return {
        "beantwortbar": block(answerable),
        "ohne_antwort": {
            "n": len(no_answer),
            "korrekt_leer": (sum(1 for r in no_answer if not r["hits"]) / len(no_answer)) if no_answer else None,
        },
        "nach_typ": by_type,
        "sekunden_median": statistics.median(lat) if lat else None,
        "sekunden_max": max(lat) if lat else None,
    }


async def run(label: str) -> tuple[list[dict], dict]:
    from rag_backend import agent
    from rag_backend import ingest as ingest_mod
    from rag_backend.database import engine
    from rag_backend.models import Base

    Base.metadata.create_all(engine)
    await agent._ensure_models_loaded()
    collection = os.environ["QDRANT_COLLECTION"]
    ingest_mod.ensure_qdrant_collection(agent._qdrant_client, collection, int(os.getenv("EMBEDDING_DIM", 1024)))

    rows: list[dict] = []
    try:
        corpus = sorted((HERE / "corpus").glob("*.md"))
        t0 = time.monotonic()
        for p in corpus:
            res = await agent._store_text_in_qdrant(p.name, p.read_text(encoding="utf-8"), source=f"eval/{p.name}")
            if res.startswith("ERROR"):
                raise SystemExit(f"Indexieren fehlgeschlagen ({p.name}): {res}")
        index_seconds = time.monotonic() - t0

        queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
        for q in queries:
            t = time.monotonic()
            out = await agent.document_search.ainvoke({"query": q["query"]})
            dt = time.monotonic() - t
            if out.startswith("ERROR"):
                raise SystemExit(f"Suche fehlgeschlagen ({q['id']}): {out[:300]}")
            hits = parse_hits(out)
            relevant = set(q["relevant"])
            rank = next((i + 1 for i, (src, _) in enumerate(hits) if src in relevant), None)
            rows.append({
                "id": q["id"], "type": q["type"], "query": q["query"], "relevant": q["relevant"],
                "hits": hits, "rank": rank, "seconds": round(dt, 3),
            })
    finally:
        try:
            agent._qdrant_client.delete_collection(collection)
        except Exception as e:
            print(f"Warnung: Sammlung {collection} nicht geloescht: {e}")

    summary = summarize(rows)
    summary["label"] = label
    summary["dokumente"] = len(corpus)
    summary["indexieren_sekunden"] = round(index_seconds, 1)
    return rows, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=WERT")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    for item in args.set:
        k, _, v = item.partition("=")
        os.environ[k] = v
    os.environ["QDRANT_COLLECTION"] = f"argus_eval_{args.label}_{uuid.uuid4().hex[:6]}"
    # Wegwerf-Datenbank statt Postgres; kein Tracing.
    os.environ["DATABASE_URL"] = "sqlite:////tmp/retrieval_eval.db"
    os.environ["POSTGRES_USER"] = ""
    os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = ""
    os.environ.setdefault("PII_BLOCKLIST_PATH", "/nonexistent")

    rows, summary = asyncio.run(run(args.label))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in rows:
        if r["relevant"] and r["rank"] != 1:
            top = ", ".join(f"{s} {sc:.2f}" for s, sc in r["hits"][:3]) or "leer"
            print(f"  verfehlt {r['id']} [{r['type']}] Rang {r['rank']}: {r['query'][:70]} -> {top}")
        if not r["relevant"] and r["hits"]:
            print(f"  falsch  {r['id']} [no_answer]: {r['query'][:70]} -> {r['hits'][0][0]} {r['hits'][0][1]:.2f}")


if __name__ == "__main__":
    main()
