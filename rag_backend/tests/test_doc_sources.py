"""Dokumente der Dokumentensuche als Quellenangabe im Chat.

Die Angabe kommt aus dem Suchergebnis, nicht aus dem Text des Modells. Das
Werkzeug-Ergebnis, das das Modell sieht, darf sich dadurch nicht aendern
(sonst gelten die Messungen in eval/agent nicht mehr)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


def test_dokumente_entdoppelt_und_nach_relevanz_sortiert():
    from rag_backend import agent

    agent.dokumente_zuruecksetzen()
    agent.dokumente_merken([
        ({"source": "b.pdf", "chunk_index": 0}, 0.40),
        ({"source": "a.pdf", "chunk_index": 3}, 0.91),
        ({"source": "b.pdf", "chunk_index": 2}, 0.72),
        ({"source": "", "chunk_index": 0}, 0.99),
    ])
    # Zweite Suche im selben Turn: schlechterer Wert ueberschreibt nicht.
    agent.dokumente_merken([({"source": "a.pdf"}, 0.10), ({"source": "c.md"}, "kaputt")])
    assert agent.dokumente_liste() == [
        {"name": "a.pdf", "score": 0.91},
        {"name": "b.pdf", "score": 0.72},
        {"name": "c.md", "score": 0.0},
    ]


def test_dokumente_neuer_turn_beginnt_leer():
    from rag_backend import agent

    agent.dokumente_zuruecksetzen()
    agent.dokumente_merken([({"source": "alt.pdf"}, 0.5)])
    agent.dokumente_zuruecksetzen()
    assert agent.dokumente_liste() == []


def test_dokumente_ohne_register_geschieht_nichts():
    import contextvars

    from rag_backend import agent

    def _lauf():
        # Frischer Kontext ohne Turn: kein Register, kein Fehler, keine Liste.
        agent.dokumente_merken([({"source": "x.pdf"}, 0.9)])
        return agent.dokumente_liste()

    assert contextvars.Context().run(_lauf) == []


@pytest.mark.asyncio
@patch("rag_backend.agent._http_client_web", MagicMock())
@patch("rag_backend.agent._http_client_internal", MagicMock())
@patch("rag_backend.agent._qdrant_client")
async def test_document_search_meldet_dokumente_und_laesst_ergebnis_gleich(mock_qdrant, monkeypatch):
    from rag_backend import agent, retrieval

    async def _embed(client, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def _rerank(client, query, docs):
        return [0.95 for _ in docs]

    monkeypatch.setattr(retrieval, "embed", _embed)
    monkeypatch.setattr(retrieval, "rerank", _rerank)

    hit = MagicMock()
    hit.payload = {"text": "Testinhalt aus Qdrant.", "original_filename": "bericht_8d.pdf"}
    antwort = MagicMock()
    antwort.points = [hit]
    mock_qdrant.query_points.return_value = antwort

    #--- Ohne Register (kein Chat-Turn) und mit Register: derselbe Text fuer das Modell.
    ohne = await agent.document_search.ainvoke({"query": "Test"})
    agent.dokumente_zuruecksetzen()
    mit = await agent.document_search.ainvoke({"query": "Test"})
    assert mit == ohne
    assert "source=\"bericht_8d.pdf\"" in mit
    assert "Gefunden in" not in mit

    assert agent.dokumente_liste() == [{"name": "bericht_8d.pdf", "score": 0.95}]


def test_backend_reicht_dokumente_weiter_und_speichert_sie():
    quelltext = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert '{"type": "doc_sources", "items": dsrcs}' in quelltext
    assert '"dokumente": dokumente' in quelltext


def test_oberflaeche_zeigt_dokumente_ohne_innerhtml():
    voice = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    assert "ev.type === 'doc_sources'" in voice
    assert "onDocSources" in chat

    anfang = chat.index("function chatAddDocSources")
    ende = chat.index("\n}\n", anfang)
    funktion = chat[anfang:ende]
    assert "innerHTML" not in funktion
    assert "createTextNode" in funktion
    #--- Ein neu geladener Verlauf zeigt die Dokumente wieder.
    assert "chatAddDocSources(msg, steps.dokumente || [])" in chat
