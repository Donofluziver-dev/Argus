import base64
import codecs
import gzip
import os
import time
from unittest.mock import MagicMock

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("CLOUD_REDACTION_LLM_PASS", "false")

import pytest

from rag_backend import agent, egress_guard

_SECRET_DOC = (
    "Mitarbeiter: Maximilian Muster (E-Mail: max.muster@intern-nordstern.de)\n"
    "Kundennummer: KD-889912, Rahmenvertrag: VTR-2026-49102\n"
    "Bankkonto IBAN: DE89 3704 0044 0532 0130 00\n"
    "Vertrauliche Notiz: Die geheime Fusion der Konzerne ist fuer November geplant."
)


@pytest.fixture(autouse=True)
def _clean_egress_state(monkeypatch):
    """Stellt vor und nach jedem Test einen sauberen Egress-Zustand her."""
    egress_guard.reset_egress_state()
    agent.request_origin_var.set({})
    agent.subagent_depth_var.set(0)
    monkeypatch.delenv("EGRESS_AFTER_SENSITIVE", raising=False)
    monkeypatch.delenv("CLOUD_MISSION_AUTO_APPROVE", raising=False)
    yield
    egress_guard.reset_egress_state()
    agent.request_origin_var.set({})
    agent.subagent_depth_var.set(0)


# ---------------------------------------------------------------------------
# 1. Base64 (Standard mit Auffuellung =)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_base64_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    b64_val = base64.b64encode(b"VTR-2026-49102").decode()
    url = f"https://example.com/search?q={b64_val}"

    assert await egress_guard.check_egress_allowed("web_search", url) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(url)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 2. Base64 ohne Auffuellung
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_base64_unpadded_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    b64_unpadded = base64.b64encode(b"VTR-2026-49102").decode().rstrip("=")
    query = f"Suche nach {b64_unpadded} im Firmenarchiv"

    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 3. URL-sicher (urlsafe base64)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_urlsafe_base64_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    b64_urlsafe = base64.urlsafe_b64encode(b"VTR-2026-49102").decode()
    url = f"https://api.example.com/check?param={b64_urlsafe}"

    assert await egress_guard.check_egress_allowed("web_search", url) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(url)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 4. Base32 (mit und ohne Auffuellung)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_base32_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    b32_padded = base64.b32encode(b"VTR-2026-49102").decode()
    url_pad = f"https://api.example.com/fetch?id={b32_padded}"
    assert await egress_guard.check_egress_allowed("web_search", url_pad) == egress_guard.CONTENT_BLOCK_MESSAGE

    b32_unpadded = b32_padded.rstrip("=")
    url_unpad = f"https://api.example.com/fetch?id={b32_unpadded}"
    assert await egress_guard.check_egress_allowed("web_search", url_unpad) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(url_unpad)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 5. Hex mit und ohne Trenner (0x, \x, Leerzeichen, Doppelpunkt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    b"VTR-2026-49102".hex(),  # ohne Trenner
    " ".join(f"{b:02x}" for b in b"VTR-2026-49102"),  # Leerzeichen
    ":".join(f"{b:02x}" for b in b"VTR-2026-49102"),  # Doppelpunkt
    " ".join(f"0x{b:02x}" for b in b"VTR-2026-49102"),  # 0x getrennt
    "".join(f"\\x{b:02x}" for b in b"VTR-2026-49102"),  # \x getrennt
])
async def test_encoding_hex_variants_blocked(monkeypatch, payload):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    query = f"Pruefe Kennung {payload} im System"
    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 6. gzip+Base64
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_gzip_base64_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    gz_bytes = gzip.compress(b"geheime fusion der konzerne")
    gz_b64 = base64.b64encode(gz_bytes).decode()
    url = f"https://external-cloud.example/sync?blob={gz_b64}"

    assert await egress_guard.check_egress_allowed("web_search", url) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(url)
    assert hit is True and reason == "content_match:phrase"


# ---------------------------------------------------------------------------
# 7. Base64 von Hex (verschachtelt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_base64_of_hex_nested_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    hex_data = b"KD-889912".hex()
    b64_hex = base64.b64encode(hex_data.encode()).decode()
    query = f"Suche nach {b64_hex}"

    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 8. ROT13
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_rot13_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    rot_email = codecs.encode("max.muster@intern-nordstern.de", "rot_13")
    query = f"Nachricht an {rot_email} senden"

    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:email"


# ---------------------------------------------------------------------------
# 9. Rueckwaerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_reversed_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    rev_id = "VTR-2026-49102"[::-1]
    query = f"ID-Abfrage fuer {rev_id}"

    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 10. Vollbreite (NFKC)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_fullwidth_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    fw_id = "ＶＴＲ－２０２６－４９１０２"
    query = f"Vertragspruefung fuer {fw_id}"

    assert await egress_guard.check_egress_allowed("web_search", query) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(query)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 11. Zeichenweise getrennt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_spaced_characters_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    spaced_iban = "D E 8 9 3 7 0 4 0 0 4 4 0 5 3 2 0 1 3 0 0 0"
    query_iban = f"Kontenabgleich fuer {spaced_iban}"
    assert await egress_guard.check_egress_allowed("web_search", query_iban) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit_iban, reason_iban = egress_guard.check_egress_content(query_iban)
    assert hit_iban is True and reason_iban == "content_match:iban"

    spaced_dots = "K.D.8.8.9.9.1.2"
    query_dots = f"Kunde {spaced_dots} pruefen"
    assert await egress_guard.check_egress_allowed("web_search", query_dots) == egress_guard.CONTENT_BLOCK_MESSAGE

    spaced_dashes = "K-D-8-8-9-9-1-2"
    query_dashes = f"Kunde {spaced_dashes} pruefen"
    assert await egress_guard.check_egress_allowed("web_search", query_dashes) == egress_guard.CONTENT_BLOCK_MESSAGE


# ---------------------------------------------------------------------------
# 12. URL-kodiertes Base64
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encoding_url_encoded_base64_blocked(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    b64_val = base64.b64encode(b"VTR-2026-49102").decode()
    url_enc_b64 = b64_val.replace("=", "%3D")
    url = f"https://example.com/api?token={url_enc_b64}"

    assert await egress_guard.check_egress_allowed("web_search", url) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit, reason = egress_guard.check_egress_content(url)
    assert hit is True and reason == "content_match:id"


# ---------------------------------------------------------------------------
# 13. Verschluesselte Folge (100 Zeichen Zufalls-Base64)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encrypted_blob_blocked_in_marked_and_allowed_in_unmarked(monkeypatch):
    rand_blob = base64.b64encode(os.urandom(75)).decode()
    assert len(rand_blob) == 100

    # In markiertem Gespraech gesperrt mit content_match:encoded_blob
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)
    assert await egress_guard.check_egress_allowed("web_search", rand_blob) == egress_guard.CONTENT_BLOCK_MESSAGE
    hit_marked, reason_marked = egress_guard.check_egress_content(rand_blob)
    assert hit_marked is True and reason_marked == "content_match:encoded_blob"

    # In unmarkiertem Gespraech NICHT gesperrt
    egress_guard.reset_egress_state()
    assert await egress_guard.check_egress_allowed("web_search", rand_blob) is None
    hit_unmarked, reason_unmarked = egress_guard.check_egress_content(rand_blob)
    assert hit_unmarked is False and reason_unmarked == ""


# ---------------------------------------------------------------------------
# 14. Harmlose Faelle (Websuche, UUID, Git-Hash, SHA-256)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benign_cases_allowed_in_marked_conversation(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    # 1. Normale Websuche
    assert await egress_guard.check_egress_allowed("web_search", "Wetter morgen in Hamburg") is None

    # 2. URL mit UUID
    uuid_url = "https://example.com/api/v1/users/c9a646d3-9c61-4cd9-bc19-470423c321aa"
    assert await egress_guard.check_egress_allowed("web_search", uuid_url) is None

    # 3. Git-Hash (40 Hex)
    git_query = "Commit 4b825dc642cb6eb9a060e54bf8d69288fbee4904 im Repo pruefen"
    assert await egress_guard.check_egress_allowed("web_search", git_query) is None

    # 4. SHA-256 (64 Hex) als eigenes Wort
    sha_query = "Prüfe Prüfsumme e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 fuer Download"
    assert await egress_guard.check_egress_allowed("web_search", sha_query) is None

    # 5. Gewoehnliche Seiten: lange Pfade und Titel bestehen aus Zeichen des
    #    Base64-Alphabets, sind aber keine verschluesselten Daten.
    for url in (
        "https://www.heise.de/news/Microsoft-kuendigt-neue-Sicherheitsfunktionen-fuer-"
        "Entra-ID-an-und-erweitert-Defender-10123456.html",
        "https://example.com/2026/11/25/wirtschaft/unternehmen/mittelstand-investiert-"
        "in-kuenstliche-intelligenz-trotz-unsicherheit",
        "https://de.wikipedia.org/wiki/Donaudampfschifffahrtselektrizitaetenhauptbetriebs"
        "werkbauunterbeamtengesellschaft",
        "https://www.google.com/search?q=wetter+berlin&sca_esv=a1b2c3d4e5f6&source=hp",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
    ):
        assert await egress_guard.check_egress_allowed("fetch_url", {"url": url}) is None, url


@pytest.mark.asyncio
async def test_long_plain_cloud_request_is_checked_not_refused(monkeypatch):
    '''Eine lange, gewoehnliche Anfrage (Kontext einer Mission) laeuft durch die
    Pruefung, ohne an der Obergrenze der Varianten zu scheitern, und bleibt schnell.'''
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)
    absatz = ("Der Markt fuer industrielle Pumpen waechst seit Jahren stetig, getrieben von "
              "Energieeffizienz, neuen Normen und dem Ausbau der Wasserinfrastruktur. ")
    args = {"goal": "Marktueberblick Industriepumpen Europa 2026 mit Quellen",
            "context": absatz * 120, "provider": ""}
    t0 = time.perf_counter()
    hit, reason = egress_guard.check_egress_content(args)
    assert (hit, reason) == (False, "")
    assert time.perf_counter() - t0 < 2.0
    #--- Dieselbe Anfrage mit einer Firmendaten-Kennung darin wird erkannt.
    args["context"] += " Bezug: Rahmenvertrag VTR-2026-49102."
    assert egress_guard.check_egress_content(args) == (True, "content_match:id")


# ---------------------------------------------------------------------------
# 15. Zip-Bombe blockiert nicht laenger als eine Sekunde
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zip_bomb_bounded_runtime(monkeypatch):
    bomb_bytes = gzip.compress(b"\x00" * (50 * 1024 * 1024))
    bomb_b64 = base64.b64encode(bomb_bytes).decode()

    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    t0 = time.perf_counter()
    res = await egress_guard.check_egress_allowed("web_search", bomb_b64)
    dt = time.perf_counter() - t0

    assert dt < 1.0, f"Zip-Bombe dauerte zu lange: {dt:.2f} s"
    assert res == egress_guard.CONTENT_BLOCK_MESSAGE
    assert egress_guard.check_egress_content(bomb_b64) == (True, "content_match:too_large")


@pytest.mark.asyncio
async def test_secret_behind_padding_in_compressed_data_is_blocked(monkeypatch):
    '''Firmendaten hinter mehr als 1 MB Fuellmaterial: Was ueber die Grenze hinausgeht,
    bliebe ungeprueft, also wird gesperrt.'''
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)
    payload = base64.b64encode(gzip.compress(b"a " * 600_000 + b"VTR-2026-49102")).decode()
    assert egress_guard.check_egress_content(payload) == (True, "content_match:too_large")


# ---------------------------------------------------------------------------
# 16. Laufzeit einer normalen Suchanfrage unter 20 ms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benign_query_fast_latency(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    t0 = time.perf_counter()
    res = await egress_guard.check_egress_allowed("web_search", "Aktuelle Börsenkurse Frankfurt")
    dt = time.perf_counter() - t0

    #--- Grobe Schranke mit Spielraum fuer langsame CI-Rechner (lokal wenige ms).
    assert dt < 0.05, f"Normale Suchanfrage dauerte zu lange: {dt*1000:.2f} ms"
    assert res is None


# ---------------------------------------------------------------------------
# 17. Eingabe ueber 200 kB sperrt mit content_match:too_large
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_argument_too_large_blocks(monkeypatch):
    big_arg = "X" * 200_001
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    hit, reason = egress_guard.check_egress_content(big_arg)
    assert hit is True and reason == "content_match:too_large"
    assert await egress_guard.check_egress_allowed("web_search", big_arg) == egress_guard.CONTENT_BLOCK_MESSAGE


# ---------------------------------------------------------------------------
# 18. Fail-closed bei Ausnahmen (content_match:check_failed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("read_document", _SECRET_DOC)

    monkeypatch.setattr(egress_guard, "_flatten_strings", MagicMock(side_effect=RuntimeError("Simulierter Absturz")))
    hit, reason = egress_guard.check_egress_content("suche")
    assert hit is True and reason == "content_match:check_failed"
    assert await egress_guard.check_egress_allowed("web_search", "suche") == egress_guard.CONTENT_BLOCK_MESSAGE
