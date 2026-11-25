"""Bildeingabe: Bilder und gescannte PDF-Seiten liest das lokale Modell selbst."""
import io
import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pypdfium2 as pdfium
import pytest
from PIL import Image

from rag_backend import agent, egress_guard, ingest, vision
from rag_backend.tests.test_main import (  # noqa: F401
    _make_account,
    override_get_db,
    test_client,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    vision.reset_cache()
    monkeypatch.delenv("LLM_ENABLE_MULTIMODAL", raising=False)
    monkeypatch.delenv("SGLANG_ENABLE_MULTIMODAL", raising=False)
    monkeypatch.setenv("OAI_BASE_URL", "http://llm:8080/v1")
    monkeypatch.setattr("rag_backend.crypto_utils.load_llm_api_key", lambda: "k")
    yield
    vision.reset_cache()


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _props(monkeypatch, *, vision_on=None, fail=False):
    calls = []

    def _get(url, **kw):
        calls.append(url)
        if fail:
            raise OSError("LLM-Server nicht erreichbar")
        mods = {} if vision_on is None else {"vision": vision_on}
        return _Resp(200, {"modalities": mods})

    monkeypatch.setattr(vision.httpx, "get", _get)
    return calls


# ---- Ist die Bildeingabe da? -------------------------------------------------

def test_switch_forces_on_and_off(monkeypatch):
    calls = _props(monkeypatch, vision_on=True)
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "false")
    assert vision.vision_available() is False
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
    assert vision.vision_available() is True
    assert calls == []


def test_auto_asks_the_model_server_and_caches(monkeypatch):
    calls = _props(monkeypatch, vision_on=True)
    assert vision.vision_available() is True
    assert vision.vision_available() is True
    assert calls == ["http://llm:8080/props"]


def test_old_sglang_switch_is_ignored(monkeypatch):
    #--- Aeltere .env-Dateien tragen noch SGLANG_ENABLE_MULTIMODAL=false.
    _props(monkeypatch, vision_on=True)
    monkeypatch.setenv("SGLANG_ENABLE_MULTIMODAL", "false")
    assert vision.vision_available() is True


def test_auto_without_encoder_is_off(monkeypatch):
    _props(monkeypatch, vision_on=None)
    assert vision.vision_available() is False


def test_unreachable_server_is_off_and_asked_again_soon(monkeypatch):
    calls = _props(monkeypatch, fail=True)
    clock = [1000.0]
    monkeypatch.setattr(vision.time, "monotonic", lambda: clock[0])
    assert vision.vision_available() is False
    clock[0] += 10
    assert vision.vision_available() is False and len(calls) == 1
    clock[0] += 6                                   # nach 15 s neu fragen
    assert vision.vision_available() is False and len(calls) == 2


# ---- Abschreiben -------------------------------------------------------------

def _png(color="white", size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_transcription_goes_to_the_local_model_without_thinking(monkeypatch):
    sent = {}

    def _post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return _Resp(200, {"choices": [{"message": {"content": "<think>x</think>Rechnung 4711"}}]})

    monkeypatch.setattr(vision.httpx, "post", _post)
    monkeypatch.setenv("VISION_MAX_SIDE", "400")
    assert vision.transcribe_image(_png(size=(3000, 1000))) == "Rechnung 4711"
    assert sent["url"] == "http://llm:8080/v1/chat/completions"
    body = sent["json"]
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    image = body["messages"][1]["content"][0]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")
    #--- Auf die laengste Seite verkleinert.
    import base64
    raw = base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    assert max(Image.open(io.BytesIO(raw)).size) == 400


# ---- Gescannte PDFs ----------------------------------------------------------

def _text_pdf(text: str) -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font "
            b"<< /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _scan_pdf(pages: int) -> bytes:
    images = [Image.new("RGB", (600, 800), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def _pdf(tmp_path, *parts) -> "os.PathLike":
    doc = pdfium.PdfDocument.new()
    for data in parts:
        doc.import_pages(pdfium.PdfDocument(data))
    path = tmp_path / "akte.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _fake_reader(monkeypatch, fail_pages=()):
    seen = []

    def _transcribe(png):
        seen.append(len(seen) + 1)
        if len(seen) in fail_pages:
            raise RuntimeError("Zeitlimit")
        return f"ABGESCHRIEBEN {len(seen)}"

    monkeypatch.setattr(vision, "transcribe_image", _transcribe)
    return seen


def test_scanned_page_is_read_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
    _fake_reader(monkeypatch)
    path = _pdf(tmp_path, _text_pdf("Rechnung Nr. 4711 vom 3. Maerz 2026"), _scan_pdf(1))
    text = ingest.load_file(path)
    first, second = text.index("Rechnung Nr. 4711"), text.index("--- Seite 2 (aus dem Bild gelesen) ---")
    assert first < second and "ABGESCHRIEBEN 1" in text[second:]


def test_without_image_input_scans_stay_unread(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "false")
    seen = _fake_reader(monkeypatch)
    path = _pdf(tmp_path, _text_pdf("Rechnung Nr. 4711 vom 3. Maerz 2026"), _scan_pdf(1))
    text = ingest.load_file(path)
    assert "Rechnung Nr. 4711" in text and "ABGESCHRIEBEN" not in text and seen == []
    #--- Ein reines Scan-PDF bleibt ohne Bildeingabe leer.
    assert not (ingest.load_file(_pdf(tmp_path, _scan_pdf(2))) or "").strip()


def test_page_limit_and_failing_page(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
    monkeypatch.setenv("SCAN_MAX_PAGES", "2")
    _fake_reader(monkeypatch, fail_pages=(1,))
    text = ingest.load_file(_pdf(tmp_path, _scan_pdf(4)))
    assert "[Seite 1 nicht lesbar]" in text
    assert "--- Seite 2 (aus dem Bild gelesen) ---\nABGESCHRIEBEN 2" in text
    assert "Seite 3: gescannt, nicht gelesen (Grenze erreicht; 2 Scan-Seite(n) fehlen)" in text
    assert "Seite 4" not in text


def test_image_file_with_and_without_image_input(tmp_path, monkeypatch):
    path = tmp_path / "foto.png"
    path.write_bytes(_png())
    _fake_reader(monkeypatch)
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "false")
    assert ingest.load_file(path) is None
    assert ".png" not in ingest.readable_extensions()
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
    assert ingest.load_file(path) == "--- Bild foto.png (aus dem Bild gelesen) ---\nABGESCHRIEBEN 1"
    assert ".png" in ingest.readable_extensions()


# ---- read_document -----------------------------------------------------------

@pytest.mark.asyncio
async def test_read_document_reads_images_as_company_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(agent, "_WORKSPACE_CONTAINER", tmp_path)
    agent.request_origin_var.set({"user_role": "admin", "user_id": 1})
    egress_guard.reset_egress_state()
    (tmp_path / "Uploads").mkdir()
    img = tmp_path / "Uploads" / "scan.png"
    img.write_bytes(_png())
    _fake_reader(monkeypatch)
    try:
        monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
        res = await agent.read_document.ainvoke({"path": str(img)})
        assert "ABGESCHRIEBEN 1" in res and not res.startswith("ERROR")
        assert egress_guard.is_conversation_sensitive()

        monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "false")
        res = await agent.read_document.ainvoke({"path": str(img)})
        assert res.startswith("ERROR") and "LLM_MMPROJ_FILE" in res
    finally:
        agent.request_origin_var.set({})
        egress_guard.reset_egress_state()


# ---- Chat-Ansicht ------------------------------------------------------------

def test_config_reports_image_input(client, monkeypatch):
    _make_account(client, "bilder", "einLangesPasswort", "admin")
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "true")
    assert client.get("/v1/dashboard/config").json()["config"]["multimodal"] is True
    monkeypatch.setenv("LLM_ENABLE_MULTIMODAL", "false")
    assert client.get("/v1/dashboard/config").json()["config"]["multimodal"] is False
