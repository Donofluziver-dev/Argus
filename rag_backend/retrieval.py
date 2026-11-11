"""Dokumentensuche: Hybridsuche ueber Qdrant mit den Containern embed und rerank.

- Bedeutung: bge-m3 im Container embed (llama.cpp, /v1/embeddings) als dichter
  Vektor "dense".
- Stichworte: BM25 als Sparse-Vektor "bm25". Die Termgewichte rechnet dieser
  Prozess, die IDF rechnet Qdrant selbst (Modifier.IDF). Findet genaue Namen,
  Teile- und Vertragsnummern, die eine reine Bedeutungssuche oft verfehlt.
- Qdrant fuehrt beide Vorauswahlen per Reciprocal Rank Fusion zusammen.
- bge-reranker-v2-m3 im Container rerank (/v1/rerank) bewertet die besten
  Kandidaten. llama.cpp liefert rohe Logits; nach aussen gehen sie per Sigmoid
  als 0..1, damit HARD_MIN_SCORE und die Anzeige "relevance" gleich bleiben.

Faellt einer der Container aus, sucht der Rest weiter (nur Stichworte bzw.
Reihenfolge der Vorauswahl) und meldet das als Hinweis.

Frueher lud das Backend beide Modelle zusaetzlich selbst ueber PyTorch, obwohl
die Container liefen: gut 4 GB RAM doppelt und ein Download bei jedem Neubau.
"""

from __future__ import annotations

import logging
import math
import os
import re
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass, field

import httpx
from qdrant_client import QdrantClient, models

log = logging.getLogger(__name__)

DENSE = "dense"
SPARSE = "bm25"

# BM25-Parameter wie ueblich; die mittlere Abschnittslaenge ist fest (CHUNK_SIZE
# 1536 Zeichen sind gut 200 Woerter), Qdrant kennt sie nicht.
_K1 = 1.2
_B = 0.75
_AVG_LEN = 200.0

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Kennungen wie HA-40817-K oder HAT-V-2025-0412 zusaetzlich am Stueck, damit
# fast gleiche Nummern sich klar unterscheiden.
_COMPOUND_RE = re.compile(r"[^\W_]+(?:[-./_][^\W_]+)+", re.UNICODE)


def _settings() -> dict:
    return {
        "embed_url": os.getenv("EMBED_URL", "http://embed:8080/v1").rstrip("/"),
        "rerank_url": os.getenv("RERANK_URL", "http://rerank:8080/v1").rstrip("/"),
        # Eigener Name statt K_RETRIEVAL: das hiess frueher "Treffer der
        # Vektorsuche" (8) und wuerde in alten .env-Dateien die Vorauswahl kappen.
        "per_method": int(os.getenv("K_PER_METHOD", 40)),
        # 12 statt 24: gleiche Trefferquote im Testbestand, halbe Dauer (eval/retrieval).
        "candidates": int(os.getenv("RERANK_CANDIDATES", 12)),
        "embed_batch": int(os.getenv("EMBED_BATCH", 16)),
    }


# ---------------------------------------------------------------------------
# BM25 als Sparse-Vektor
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    low = (text or "").lower()
    tokens = [t for t in _WORD_RE.findall(low) if len(t) > 1 or t.isdigit()]
    for m in _COMPOUND_RE.finditer(low):
        if any(c.isdigit() for c in m.group(0)):
            tokens.append(re.sub(r"[-./_]", "", m.group(0)))
    return tokens


def _term_id(term: str) -> int:
    return zlib.crc32(term.encode("utf-8"))


def _sparse(weights: dict[int, float]) -> models.SparseVector:
    idx = sorted(weights)
    return models.SparseVector(indices=idx, values=[weights[i] for i in idx])


def document_sparse(text: str) -> models.SparseVector:
    """Termgewichte eines Abschnitts (BM25 ohne IDF)."""
    tokens = tokenize(text)
    norm = _K1 * (1 - _B + _B * len(tokens) / _AVG_LEN)
    weights: dict[int, float] = {}
    for term, tf in Counter(tokens).items():
        tid = _term_id(term)
        weights[tid] = weights.get(tid, 0.0) + tf * (_K1 + 1) / (tf + norm)
    return _sparse(weights)


def query_sparse(text: str) -> models.SparseVector | None:
    weights = {_term_id(t): 1.0 for t in set(tokenize(text))}
    return _sparse(weights) if weights else None


def chunk_header(filename: str) -> str:
    """Dateiname als Kopfzeile: hilft, wenn der Nutzer ein Dokument beim Namen nennt."""
    stem = re.sub(r"\.[a-z0-9]{1,5}$", "", filename or "", flags=re.I)
    return "Dokument: " + re.sub(r"[_\-]+", " ", stem).strip()


# ---------------------------------------------------------------------------
# Container embed und rerank
# ---------------------------------------------------------------------------

def _headers() -> dict:
    from rag_backend.crypto_utils import load_llm_api_key

    key = load_llm_api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _parse_embeddings(data: dict, n: int) -> list[list[float]]:
    items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
    if len(items) != n:
        raise RuntimeError(f"Embedding-Dienst lieferte {len(items)} statt {n} Vektoren")
    return [d["embedding"] for d in items]


async def embed(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    s = _settings()
    out: list[list[float]] = []
    for i in range(0, len(texts), s["embed_batch"]):
        batch = texts[i:i + s["embed_batch"]]
        r = await client.post(f"{s['embed_url']}/embeddings", headers=_headers(),
                              json={"input": batch, "model": "bge-m3"}, timeout=120.0)
        r.raise_for_status()
        out.extend(_parse_embeddings(r.json(), len(batch)))
    return out


def embed_sync(texts: list[str]) -> list[list[float]]:
    """Fuer den Batch-Ingest von der Kommandozeile (ingest.py)."""
    s = _settings()
    out: list[list[float]] = []
    with httpx.Client(timeout=120.0) as client:
        for i in range(0, len(texts), s["embed_batch"]):
            batch = texts[i:i + s["embed_batch"]]
            r = client.post(f"{s['embed_url']}/embeddings", headers=_headers(),
                            json={"input": batch, "model": "bge-m3"})
            r.raise_for_status()
            out.extend(_parse_embeddings(r.json(), len(batch)))
    return out


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


async def rerank(client: httpx.AsyncClient, query: str, documents: list[str]) -> list[float]:
    """Bewertung 0..1 je Dokument in Eingabereihenfolge."""
    if not documents:
        return []
    s = _settings()
    r = await client.post(f"{s['rerank_url']}/rerank", headers=_headers(),
                          json={"model": "bge-reranker-v2-m3", "query": query,
                                "documents": documents, "top_n": len(documents)},
                          timeout=120.0)
    r.raise_for_status()
    scores = [0.0] * len(documents)
    for item in r.json().get("results") or []:
        i = item.get("index")
        if isinstance(i, int) and 0 <= i < len(documents):
            scores[i] = _sigmoid(float(item.get("relevance_score", -50.0)))
    return scores


# ---------------------------------------------------------------------------
# Sammlung anlegen und alten Aufbau umziehen
# ---------------------------------------------------------------------------

_PAYLOAD_INDEXES = [
    ("original_filename", models.PayloadSchemaType.KEYWORD),
    ("source", models.PayloadSchemaType.KEYWORD),
    ("chunk_index", models.PayloadSchemaType.INTEGER),
    ("language", models.PayloadSchemaType.KEYWORD),
    ("created_at", models.PayloadSchemaType.DATETIME),
    ("success_score", models.PayloadSchemaType.FLOAT),
]


def _create(client: QdrantClient, name: str, dim: int) -> None:
    client.create_collection(
        collection_name=name,
        vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    for field_name, schema in _PAYLOAD_INDEXES:
        try:
            client.create_payload_index(collection_name=name, field_name=field_name, field_schema=schema)
        except Exception as e:
            log.warning(f"Payload-Index '{field_name}' fuer {name} nicht erstellt: {e}")


def is_hybrid(client: QdrantClient, name: str) -> bool:
    params = client.get_collection(name).config.params
    vectors = params.vectors
    return isinstance(vectors, dict) and DENSE in vectors and SPARSE in (params.sparse_vectors or {})


def _scroll_all(client: QdrantClient, name: str, with_vectors=False):
    offset = None
    while True:
        points, offset = client.scroll(collection_name=name, limit=128, offset=offset,
                                       with_payload=True, with_vectors=with_vectors)
        yield from points
        if offset is None:
            return


def ensure_collection(client: QdrantClient, name: str, dim: int) -> str:
    """Legt die Sammlung im Hybrid-Aufbau an oder zieht einen alten Aufbau um.

    Rueckgabe: "vorhanden", "neu" oder "umgezogen". Beim Umzug werden die Texte
    aus der Payload neu eingebettet (andere Vektoren als das fruehere PyTorch-
    Modell) und unter demselben Namen mit denselben IDs neu geschrieben. Die
    alte Sammlung wird erst geloescht, wenn die Kopie vollstaendig ist."""
    try:
        if is_hybrid(client, name):
            return "vorhanden"
    except Exception:
        _create(client, name, dim)
        return "neu"

    old_count = client.count(name, exact=True).count
    temp = f"{name}__umzug"
    try:
        client.delete_collection(temp)
    except Exception:
        pass
    _create(client, temp, dim)

    def _batches():
        batch = []
        for p in _scroll_all(client, name):
            batch.append(p)
            if len(batch) >= 64:
                yield batch
                batch = []
        if batch:
            yield batch

    for batch in _batches():
        texts = [(p.payload or {}).get("text", "") for p in batch]
        names = [(p.payload or {}).get("original_filename", "") for p in batch]
        vectors = embed_sync([f"{chunk_header(n)}\n{t}" for n, t in zip(names, texts)])
        client.upsert(collection_name=temp, wait=True, points=[
            models.PointStruct(id=p.id, payload=p.payload,
                               vector={DENSE: v, SPARSE: document_sparse(f"{chunk_header(n)}\n{t}")})
            for p, v, n, t in zip(batch, vectors, names, texts)
        ])
    if client.count(temp, exact=True).count != old_count:
        raise RuntimeError(f"Umzug von {name} unvollstaendig, alte Sammlung bleibt.")

    # Unter dem alten Namen neu anlegen und die fertigen Vektoren kopieren.
    client.delete_collection(name)
    _create(client, name, dim)
    batch = []
    for p in _scroll_all(client, temp, with_vectors=True):
        batch.append(models.PointStruct(id=p.id, payload=p.payload, vector=p.vector))
        if len(batch) >= 128:
            client.upsert(collection_name=name, points=batch, wait=True)
            batch = []
    if batch:
        client.upsert(collection_name=name, points=batch, wait=True)
    if client.count(name, exact=True).count == old_count:
        client.delete_collection(temp)
    log.info(f"Qdrant: {name} auf Hybridsuche umgezogen ({old_count} Abschnitte).")
    return "umgezogen"


def build_points(filename: str, source: str, chunks: list[str], vectors: list[list[float]],
                 extra_payload: list[dict] | None = None) -> list[models.PointStruct]:
    header = chunk_header(filename)
    points = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        payload = {"source": source, "original_filename": filename, "chunk_index": i, "text": chunk}
        if extra_payload:
            payload = {**extra_payload[i], "text": chunk}
        points.append(models.PointStruct(
            id=str(uuid.uuid4()), payload=payload,
            vector={DENSE: vec, SPARSE: document_sparse(f"{header}\n{chunk}")},
        ))
    return points


def texts_for_embedding(filename: str, chunks: list[str]) -> list[str]:
    header = chunk_header(filename)
    return [f"{header}\n{c}" for c in chunks]


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    docs: list[dict] = field(default_factory=list)   # {"text","source","chunk_index","score"}
    notes: list[str] = field(default_factory=list)


async def search(qdrant: QdrantClient, http: httpx.AsyncClient, collection: str, query: str,
                 query_filter: models.Filter | None = None) -> SearchResult:
    """Vorauswahl aus Bedeutung und Stichworten, per RRF zusammengefuehrt und vom
    Reranker bewertet; sortiert, noch ohne Schwelle."""
    import asyncio

    s = _settings()
    result = SearchResult()
    prefetch = []
    try:
        (dense_vec,) = await embed(http, [query])
        prefetch.append(models.Prefetch(query=dense_vec, using=DENSE, limit=s["per_method"], filter=query_filter))
    except Exception as e:
        log.warning(f"document_search: Embedding-Dienst nicht erreichbar: {e}")
        result.notes.append("Bedeutungssuche nicht verfuegbar, nur Stichwortsuche.")
    sparse_vec = query_sparse(query)
    if sparse_vec is not None:
        prefetch.append(models.Prefetch(query=sparse_vec, using=SPARSE, limit=s["per_method"], filter=query_filter))
    if not prefetch:
        return result

    if len(prefetch) == 1:
        res = await asyncio.to_thread(
            qdrant.query_points, collection_name=collection, query=prefetch[0].query,
            using=prefetch[0].using, query_filter=query_filter, limit=s["candidates"], with_payload=True)
    else:
        res = await asyncio.to_thread(
            qdrant.query_points, collection_name=collection, prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=s["candidates"], with_payload=True)
    candidates = [
        {
            "text": (p.payload or {}).get("text", ""),
            "source": (p.payload or {}).get("original_filename", "unknown"),
            "chunk_index": (p.payload or {}).get("chunk_index"),
            "score": 0.0,
        }
        for p in res.points if p.payload
    ]
    if not candidates:
        return result

    try:
        scores = await rerank(http, query, [f"{chunk_header(c['source'])}\n{c['text']}" for c in candidates])
        for c, sc in zip(candidates, scores):
            c["score"] = sc
        candidates.sort(key=lambda c: c["score"], reverse=True)
    except Exception as e:
        log.warning(f"document_search: Reranker nicht erreichbar: {e}")
        result.notes.append("Reranker nicht verfuegbar, Reihenfolge der Vorauswahl.")
        # Ohne Reranker kein Massstab fuer die Schwelle: Rang als Ersatzwert.
        for rank, c in enumerate(candidates):
            c["score"] = 1.0 - rank / (len(candidates) + 1)
    result.docs = candidates
    return result
