import os

os.environ.setdefault("PII_BLOCKLIST_PATH", "/nonexistent/pii_blocklist.txt")

import httpx
import pytest
from qdrant_client import QdrantClient, models

from rag_backend import retrieval


def _vec(seed: int, dim: int = 8) -> list[float]:
    return [1.0 if i == seed % dim else 0.0 for i in range(dim)]


def test_tokenize_keeps_ids_whole_and_split():
    toks = retrieval.tokenize("Teil HA-40817-K und Vertrag HAT-V-2025-0412, Preis 12,50 EUR")
    assert "40817" in toks and "ha40817k" in toks
    assert "hatv20250412" in toks and "2025" in toks
    assert "k" not in toks  # einzelne Buchstaben ohne Ziffer fallen weg
    # fast gleiche Nummern ergeben verschiedene Terme
    assert "ha40871k" not in toks


def test_sparse_weights_saturate_and_query_is_binary():
    doc = retrieval.document_sparse("lager lager lager halle")
    weights = dict(zip(doc.indices, doc.values))
    lager, halle = retrieval._term_id("lager"), retrieval._term_id("halle")
    assert weights[lager] > weights[halle]
    assert weights[lager] < 3 * weights[halle]  # Saettigung, nicht linear
    q = retrieval.query_sparse("Lager Lager")
    assert q.values == [1.0]
    assert retrieval.query_sparse("  ,; ") is None


def test_chunk_header_from_filename():
    assert retrieval.chunk_header("reisekostenrichtlinie_2025.md") == "Dokument: reisekostenrichtlinie 2025"


def _mock_services(embed_vectors=None, rerank_logits=None, fail=None):
    calls = {"embed": 0, "rerank": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            calls["embed"] += 1
            if fail in ("embed", "both"):
                return httpx.Response(503)
            vecs = embed_vectors(body["input"]) if embed_vectors else [_vec(0) for _ in body["input"]]
            # absichtlich vertauschte Reihenfolge: der Client sortiert nach index
            data = [{"index": i, "embedding": v} for i, v in enumerate(vecs)][::-1]
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/rerank"):
            calls["rerank"] += 1
            if fail in ("rerank", "both"):
                return httpx.Response(503)
            logits = rerank_logits(body["query"], body["documents"])
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": s} for i, s in enumerate(logits)]})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


@pytest.mark.asyncio
async def test_embed_batches_and_sorts(monkeypatch):
    monkeypatch.setenv("EMBED_BATCH", "2")
    client, calls = _mock_services(embed_vectors=lambda texts: [_vec(len(t)) for t in texts])
    out = await retrieval.embed(client, ["a", "bb", "ccc"])
    assert calls["embed"] == 2
    assert out == [_vec(1), _vec(2), _vec(3)]


@pytest.mark.asyncio
async def test_rerank_returns_sigmoid_in_input_order():
    client, _ = _mock_services(rerank_logits=lambda q, docs: [3.0, -6.0])
    scores = await retrieval.rerank(client, "frage", ["passt", "passt nicht"])
    assert scores[0] == pytest.approx(0.9526, abs=1e-3)
    assert scores[1] == pytest.approx(0.00247, abs=1e-4)


def _index(qdrant, docs: dict[str, str], dim: int = 8):
    retrieval._create(qdrant, "docs", dim)
    for n, (name, text) in enumerate(docs.items()):
        points = retrieval.build_points(name, f"eval/{name}", [text], [_vec(n, dim)])
        qdrant.upsert("docs", points=points, wait=True)


@pytest.mark.asyncio
async def test_hybrid_search_finds_exact_id_despite_wrong_dense_hit():
    """Die Bedeutungssuche zeigt auf das falsche Dokument; die Stichwortsuche
    bringt das mit der genauen Teilenummer in die Kandidaten, der Reranker
    entscheidet."""
    qdrant = QdrantClient(location=":memory:")
    _index(qdrant, {
        "teileliste_a.md": "Teil HA-40817-K Lieferzeit 6 Wochen",
        "teileliste_b.md": "Teil HA-40871-K Lieferzeit 2 Wochen",
        "urlaub.md": "Urlaubsantraege bitte zwei Wochen vorher stellen",
    })

    def rerank_logits(query, documents):
        return [4.0 if "40817" in d else -5.0 for d in documents]

    client, calls = _mock_services(embed_vectors=lambda texts: [_vec(2) for _ in texts],
                                   rerank_logits=rerank_logits)
    res = await retrieval.search(qdrant, client, "docs", "Lieferzeit HA-40817-K")
    assert res.docs[0]["source"] == "teileliste_a.md"
    assert res.docs[0]["score"] > 0.9
    assert {d["source"] for d in res.docs} >= {"teileliste_a.md", "urlaub.md"}
    assert res.notes == []


@pytest.mark.asyncio
async def test_search_survives_embed_and_rerank_outage():
    qdrant = QdrantClient(location=":memory:")
    _index(qdrant, {"a.md": "Wartung Pumpe P-7 alle 500 Stunden", "b.md": "Kantine oeffnet um 11"})
    client, _ = _mock_services(fail="both")
    res = await retrieval.search(qdrant, client, "docs", "Wartung Pumpe")
    assert res.docs and res.docs[0]["source"] == "a.md"
    assert any("Stichwortsuche" in n for n in res.notes)
    assert any("Reranker" in n for n in res.notes)


@pytest.mark.asyncio
async def test_search_respects_filter():
    qdrant = QdrantClient(location=":memory:")
    _index(qdrant, {"a.md": "Pumpe P-7", "b.md": "Pumpe P-7 Ersatz"})
    client, _ = _mock_services(rerank_logits=lambda q, docs: [1.0] * len(docs))
    filt = models.Filter(must=[models.FieldCondition(key="original_filename", match=models.MatchValue(value="b.md"))])
    res = await retrieval.search(qdrant, client, "docs", "Pumpe", filt)
    assert [d["source"] for d in res.docs] == ["b.md"]


def test_ensure_collection_creates_and_migrates_old_layout(monkeypatch):
    qdrant = QdrantClient(location=":memory:")
    assert retrieval.ensure_collection(qdrant, "neu", 8) == "neu"
    assert retrieval.is_hybrid(qdrant, "neu")
    assert retrieval.ensure_collection(qdrant, "neu", 8) == "vorhanden"

    # Alter Aufbau: ein unbenannter dichter Vektor, Text in der Payload.
    qdrant.create_collection("docs", vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE))
    qdrant.upsert("docs", wait=True, points=[
        models.PointStruct(id=i, vector=_vec(i), payload={"text": f"Abschnitt {i} Teil HA-1000{i}-K",
                                                         "original_filename": f"d{i}.md", "chunk_index": 0})
        for i in range(5)
    ])
    seen = []

    def fake_embed_sync(texts):
        seen.extend(texts)
        return [_vec(7) for _ in texts]

    monkeypatch.setattr(retrieval, "embed_sync", fake_embed_sync)
    assert retrieval.ensure_collection(qdrant, "docs", 8) == "umgezogen"
    assert retrieval.is_hybrid(qdrant, "docs")
    assert qdrant.count("docs", exact=True).count == 5
    assert "docs__umzug" not in [c.name for c in qdrant.get_collections().collections]
    assert all(t.startswith("Dokument: d") for t in seen)
    p = qdrant.retrieve("docs", ids=[3], with_payload=True, with_vectors=True)[0]
    assert p.payload["original_filename"] == "d3.md"
    assert p.vector[retrieval.DENSE] == pytest.approx(_vec(7))
    assert retrieval.SPARSE in p.vector


def test_failed_migration_keeps_old_collection(monkeypatch):
    qdrant = QdrantClient(location=":memory:")
    qdrant.create_collection("docs", vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE))
    qdrant.upsert("docs", wait=True, points=[models.PointStruct(id=1, vector=_vec(1), payload={"text": "x"})])

    def broken(texts):
        raise httpx.ConnectError("embed down")

    monkeypatch.setattr(retrieval, "embed_sync", broken)
    with pytest.raises(httpx.ConnectError):
        retrieval.ensure_collection(qdrant, "docs", 8)
    assert qdrant.count("docs", exact=True).count == 1
    assert not retrieval.is_hybrid(qdrant, "docs")
