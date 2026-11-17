"""Tests fuer die Fehlerbehebungen aus Auftrag A (brief_bugs.md).

Prueft:
1. Umlaute in Dateinamen (NFC, Unicode, Paritaet mit calc_sandbox, RFC 5987 Download, NFD-Matching).
2. Dateitypen: .odt lesen (content.xml ohne Drittpaket), .doc/.rtf Abweisung,
   read_document Unterscheidung zwischen Bild, nicht unterstuetztem Typ und leer/Scan.
3. Uploads-Retention: nur bereits indexierte Dateien loeschen, unindexierte behalten.
4. STT-Grenze: 25 MB Limit im Backend, Weiterleitung von 413, 502 nur bei Ausfall.
5. SSRF/DNS-Unabhaengigkeit: (in test_security_audit.py abgesichert).
"""

import io
import os
from pathlib import Path
import time
import unicodedata
import zipfile
import pytest

from rag_backend import agent, ingest, user_files
from rag_backend import main
from rag_backend.models import IngestedDocument
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)


# ==============================================================================
# 1. Umlaute in Dateinamen
# ==============================================================================

PARITY_TEST_CASES_VALID = [
    ("Prüfbericht_Übergabe.pdf", "Prüfbericht_Übergabe.pdf"),
    ("Pru\u0308fbericht_U\u0308bergabe.pdf", "Prüfbericht_Übergabe.pdf"),  # NFD -> NFC
    ("Bericht (2026) - Entwurf_v1.0.txt", "Bericht (2026) - Entwurf_v1.0.txt"),
    ("отчет_2026.xlsx", "отчет_2026.xlsx"),
    ("日本語_メモ.txt", "日本語_メモ.txt"),
    ("bericht:mit*fragezeichen?.txt", "bericht_mit_fragezeichen_.txt"),
    ("datei.pdf. ", "datei.pdf"),
    ("notiz.txt...", "notiz.txt"),
    ("...geheim.txt", "geheim.txt"),
    ("/pfad/zu/dokument.pdf", "dokument.pdf"),
    ("..\\windows\\notiz.txt", "notiz.txt"),
]

PARITY_TEST_CASES_INVALID = [
    "",
    "   ",
    ".",
    "..",
    "...",
    "CON",
    "con.txt",
    "PRN.pdf",
    "aux.dat",
    "NUL",
    "COM1.txt",
    "com9.png",
    "lpt1.csv",
    "LPT8.tar.gz",
]


@pytest.mark.parametrize("raw, expected", PARITY_TEST_CASES_VALID)
def test_sanitize_filename_parity_valid(raw, expected):
    assert user_files.sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", PARITY_TEST_CASES_INVALID)
def test_sanitize_filename_parity_invalid(raw):
    with pytest.raises(ValueError):
        user_files.sanitize_filename(raw)


def test_sanitize_filename_parity_length_limits():
    long_ascii = "a" * 200 + ".txt"
    cleaned = user_files.sanitize_filename(long_ascii)
    assert len(cleaned) == 180
    assert len(cleaned.encode("utf-8")) <= 240

    long_umlaut = "ä" * 150 + ".txt"
    cleaned_umlaut = user_files.sanitize_filename(long_umlaut)
    assert len(cleaned_umlaut) <= 180
    assert len(cleaned_umlaut.encode("utf-8")) <= 240


def test_safe_filename_rejections():
    # Steuerzeichen
    with pytest.raises(ValueError):
        user_files.safe_filename("schad\x00code.txt")
    with pytest.raises(ValueError):
        user_files.safe_filename("linie\numbruch.txt")

    # Fuehrender Punkt
    with pytest.raises(ValueError):
        user_files.safe_filename(".env")
    with pytest.raises(ValueError):
        user_files.safe_filename(".versteckt.txt")

    # Pfadtrennung / Traversal
    with pytest.raises(ValueError):
        user_files.safe_filename("../schad.txt")
    with pytest.raises(ValueError):
        user_files.safe_filename("ordner/datei.txt")
    with pytest.raises(ValueError):
        user_files.safe_filename("stream:ads")

    # Windows-Geraetenamen
    with pytest.raises(ValueError):
        user_files.safe_filename("con.txt")
    with pytest.raises(ValueError):
        user_files.safe_filename("NUL")

    # Gueltiger Name mit Umlauten
    assert user_files.safe_filename("Prüfbericht_Übergabe.pdf") == "Prüfbericht_Übergabe.pdf"


def test_genannte_uploads_nfc_nfd_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    u1_dir = tmp_path / "users" / "1" / "Dateien"
    u1_dir.mkdir(parents=True)
    # Datei auf Platte liegt in NFC vor
    nfc_name = "Prüfbericht_Übergabe.pdf"
    (u1_dir / nfc_name).write_text("Inhalt", encoding="utf-8")

    # Text aus macOS enthaelt NFD-codierte Zeichen
    nfd_text = unicodedata.normalize("NFD", f"Bitte pruefen Sie die Datei {nfc_name} umgehend.")
    assert nfd_text != f"Bitte pruefen Sie die Datei {nfc_name} umgehend."

    found = main._genannte_uploads(nfd_text, user_id=1, role="user")
    assert nfc_name in found


def test_dashboard_user_file_download_rfc5987(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "tester_umlaut", "einLangesPasswort", "user")

    filename = "Prüfbericht_Übergabe.pdf"
    file_bytes = b"%PDF-1.4 Dummy PDF Content"

    # Hochladen
    r_up = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": (filename, file_bytes, "application/pdf")},
    )
    assert r_up.status_code == 200, r_up.text

    # Herunterladen
    r_down = client.get(
        f"/v1/dashboard/files/dateien/{filename}",
        headers=DASH_ORIGIN,
    )
    assert r_down.status_code == 200, r_down.text
    assert r_down.headers.get("x-content-type-options") == "nosniff"
    assert "application/octet-stream" in r_down.headers.get("content-type", "")

    # Content-Disposition muss ueber FileResponse(filename=...) RFC 5987 konform sein
    disp = r_down.headers.get("content-disposition", "")
    assert "attachment" in disp
    # RFC 5987 UTF-8 Codierung oder Dateiname enthalten
    assert "filename=" in disp or "filename*=" in disp


# ==============================================================================
# 2. Dateitypen (.odt, .doc/.rtf Abweisung, read_document)
# ==============================================================================

def test_odt_reading(tmp_path):
    # Gueltiges .odt (ZIP mit content.xml) erzeugen
    odt_path = tmp_path / "test_dokument.odt"
    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<office:body><office:text>'
        '<text:h text:outline-level="1">Ueberschrift des Berichts</text:h>'
        '<text:p>Erster Absatz mit <text:span>hervorgehobenem</text:span> Text.</text:p>'
        '<text:p>Zweiter Absatz mit <text:s text:c="3"/>Abstand und Tab<text:tab/>hier.</text:p>'
        '</office:text></office:body>'
        '</office:document-content>'
    )
    with zipfile.ZipFile(odt_path, "w") as zf:
        zf.writestr("content.xml", content_xml)

    extracted = ingest.load_file(odt_path)
    assert extracted is not None
    assert "Ueberschrift des Berichts" in extracted
    assert "Erster Absatz mit hervorgehobenem Text." in extracted
    assert "Zweiter Absatz mit    Abstand und Tab\thier." in extracted


def test_odt_zip_limit_check(tmp_path):
    # ZIP-Bomb-Schutz fuer ODT
    bomb_path = tmp_path / "bomb.odt"
    with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1 MB Nullen komprimiert sehr stark (hohe Ratio)
        zf.writestr("content.xml", b"\x00" * (1024 * 1024))

    # _check_zip_limits schlaegt bei extrem hoher Ratio oder Ueberschreitung an
    assert ingest.load_file(bomb_path) is None


def test_upload_rejection_doc_and_rtf(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "admin_types", "einLangesPasswort", "admin")

    # .doc Upload
    r_doc = client.post(
        "/v1/dashboard/uploads",
        headers=DASH_ORIGIN,
        files={"file": ("alt.doc", b"doc binary", "application/msword")},
    )
    assert r_doc.status_code == 415
    assert ".docx oder PDF" in r_doc.json()["detail"]

    # .rtf Upload
    r_rtf = client.post(
        "/v1/dashboard/uploads",
        headers=DASH_ORIGIN,
        files={"file": ("notiz.rtf", b"{\\rtf1 text}", "application/rtf")},
    )
    assert r_rtf.status_code == 415
    assert ".docx oder PDF" in r_rtf.json()["detail"]


@pytest.mark.asyncio
async def test_read_document_type_distinction(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    agent.request_origin_var.set({"user_role": "admin", "user_id": 1})

    ws_uploads = tmp_path / "Uploads"
    ws_uploads.mkdir(parents=True)
    monkeypatch.setattr(agent, "_WORKSPACE_CONTAINER", tmp_path)

    # 1. Bilddatei (.png)
    img_file = ws_uploads / "foto.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\n...")
    res_img = await agent.read_document.ainvoke({"path": str(img_file)})
    assert "ERROR:" in res_img
    assert "image" in res_img.lower()
    assert "cannot be read as text documents" in res_img

    # 2. Nicht unterstuetzter Dateityp (.doc / .xyz)
    unsupported_file = ws_uploads / "alt.doc"
    unsupported_file.write_bytes(b"some content")
    res_unsup = await agent.read_document.ainvoke({"path": str(unsupported_file)})
    assert "ERROR: File type '.doc' is not supported." in res_unsup
    assert "Supported readable types:" in res_unsup
    assert ".pdf" in res_unsup
    assert ".odt" in res_unsup

    # 3. Unterstuetzter Typ, aber leer / kein Text
    empty_file = ws_uploads / "leer.txt"
    empty_file.write_text("", encoding="utf-8")
    res_empty = await agent.read_document.ainvoke({"path": str(empty_file)})
    assert "ERROR: No text could be extracted from 'leer.txt'" in res_empty
    assert "scanned/image PDF or empty document" in res_empty


# ==============================================================================
# 3. Uploads des Administrators verschwinden nach 24 Stunden
# ==============================================================================

def test_sweep_uploads_once_only_deletes_indexed(tmp_path, monkeypatch, db_session):
    upload_dir = tmp_path / "Uploads"
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(main, "_UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "_UPLOAD_RETENTION_HOURS", 24.0)
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)

    # Zwei alte Dateien anlegen (48h alt)
    old_time = time.time() - 48 * 3600

    f_indexed = upload_dir / "bereits_indexiert.pdf"
    f_indexed.write_text("Inhalt A", encoding="utf-8")
    os.utime(f_indexed, (old_time, old_time))

    f_unindexed = upload_dir / "noch_nicht_indexiert.pdf"
    f_unindexed.write_text("Inhalt B", encoding="utf-8")
    os.utime(f_unindexed, (old_time, old_time))

    # Nur f_indexed in IngestedDocument eintragen
    db_session.add(IngestedDocument(
        filename="bereits_indexiert.pdf",
        file_path=str(f_indexed),
        file_hash="hash_a",
        chunk_count=3,
    ))
    db_session.commit()

    # Sweep aufrufen
    weg = main._sweep_uploads_once()
    assert weg >= 1
    assert not f_indexed.exists(), "Bereits indexierte Datei muss geloescht werden"
    assert f_unindexed.exists(), "Noch nicht indexierte Datei muss erhalten bleiben"


# ==============================================================================
# 4. Grenze fuer Sprachaufnahmen (25 MB)
# ==============================================================================

def test_stt_backend_limit_25mb(client, monkeypatch):
    _make_account(client, "voice_user", "einLangesPasswort", "user")

    # Simuliere Upload ueber 25 MB
    big_chunk = b"X" * (1024 * 1024)  # 1 MB
    # Erzeuge einen Stream/Generator ueber 26 MB
    class FakeStream:
        def __init__(self, size_mb):
            self.remaining = size_mb
        def read(self, n):
            if self.remaining <= 0:
                return b""
            self.remaining -= 1
            return big_chunk

    # Teste ueber httpx TestClient mit einem 26 MB Byte-Puffer
    # (26 * 1024 * 1024 Bytes)
    large_audio = b"0" * (26 * 1024 * 1024)
    r = client.post(
        "/v1/dashboard/stt",
        headers=DASH_ORIGIN,
        files={"file": ("large_audio.webm", large_audio, "audio/webm")},
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "Aufnahme zu gross."


def test_stt_service_413_and_502_handling(client, monkeypatch):
    _make_account(client, "voice_user_status", "einLangesPasswort", "user")
    import httpx

    # Fall A: STT-Dienst antwortet mit 413
    class Fake413Resp:
        status_code = 413
        text = "Payload Too Large"
        def raise_for_status(self):
            pass

    class FakeClient413:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kwargs):
            return Fake413Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient413)

    r_413 = client.post(
        "/v1/dashboard/stt",
        headers=DASH_ORIGIN,
        files={"file": ("stimme.webm", b"audio_bytes", "audio/webm")},
    )
    assert r_413.status_code == 413
    assert r_413.json()["detail"] == "Aufnahme zu gross fuer den STT-Dienst."

    # Fall B: STT-Dienst antwortet mit 500 / Verbindungsabbruch -> 502
    class FakeClient500:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient500)

    r_502 = client.post(
        "/v1/dashboard/stt",
        headers=DASH_ORIGIN,
        files={"file": ("stimme.webm", b"audio_bytes", "audio/webm")},
    )
    assert r_502.status_code == 502
    assert r_502.json()["detail"] == "STT-Dienst nicht erreichbar."


def test_readable_extensions_single_source():
    #--- read_document, Indexierung und load_file duerfen bei den lesbaren Typen nicht
    #--- auseinanderlaufen: jede Endung der Liste hat einen Zweig in load_file, und
    #--- der Monitor bietet nur an, was load_file lesen kann.
    import inspect
    from rag_backend import ingest
    from rag_backend.main import _INGESTABLE_EXTENSIONS
    quelle = inspect.getsource(ingest.load_file)
    for ext in ingest.READABLE_EXTENSIONS:
        assert f'"{ext}"' in quelle, f"{ext} hat keinen Zweig in load_file"
    assert _INGESTABLE_EXTENSIONS <= ingest.READABLE_EXTENSIONS


# ==============================================================================
# Pfade ohne Windows: was das Modell und der Monitor zu sehen bekommen
# ==============================================================================


def test_fs_tools_answer_with_workspace_relative_paths(tmp_path, monkeypatch):
    from rag_backend.mcp_servers import filesystem as fs_mcp
    monkeypatch.setattr(fs_mcp, "WORKSPACE_CONTAINER", tmp_path)
    monkeypatch.setattr(fs_mcp, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(fs_mcp, "_central_audit", lambda *a, **k: None)
    #--- Lesen markiert sonst das Test-Gespraech als sensibel, und die Sperre
    #--- traefe spaetere Tests (cloud_ask).
    monkeypatch.setattr("rag_backend.egress_guard.mark_sensitive_source", lambda *a, **k: None)
    (tmp_path / "Uploads").mkdir()
    (tmp_path / "Uploads" / "a.txt").write_text("x", encoding="utf-8")

    assert fs_mcp.fs_list_directory() == "Uploads/"
    assert fs_mcp.fs_list_directory("Uploads", recursive=True) == "Uploads/a.txt"
    assert fs_mcp.fs_write_file("Notizen/n.txt", "hallo").startswith("OK written: Notizen/n.txt (")
    assert fs_mcp.fs_mkdir("Neu/Unter") == "OK created: Neu/Unter"
    #--- Windows-Pfade aus frueheren Gespraechen gelten als Eingabe weiter.
    assert fs_mcp.fs_read_file(r"C:\Argus_Workspace\Uploads\a.txt") == "x"
    with pytest.raises(PermissionError, match="relative to it") as exc:
        fs_mcp.fs_read_file("/etc/passwd")
    assert "C:\\" not in str(exc.value)


@pytest.mark.asyncio
async def test_read_document_outside_workspace_names_no_windows_path():
    res = await agent.read_document.ainvoke({"path": "/etc/passwd"})
    assert res.startswith("ERROR") and "C:\\" not in res and "Argus_Workspace" not in res


def test_evals_endpoint_reports_host_dir(client, monkeypatch):
    monkeypatch.setenv("ARGUS_EVALS_DIR_HOST", "/srv/argus/evals")
    _make_account(client, "admin_evals", "einLangesPasswort", "admin")
    r = client.get("/v1/dashboard/evals", headers=DASH_ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["host_dir"] == "/srv/argus/evals"
