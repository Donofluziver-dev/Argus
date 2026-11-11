"""Beigelegte Dateien und die Sperre nach Firmendaten.

Zwei Dinge: Ein Gespraech, dessen Nachricht eine Datei aus dem Uploads-Ordner
nennt, gilt sofort als Gespraech mit Firmendaten. Und ein Werkzeugschritt, den
die Sperre abgewiesen hat, wird der Oberflaeche als gesperrt gemeldet."""
from pathlib import Path

import pytest

from rag_backend import agent, egress_guard

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


@pytest.fixture(autouse=True)
def _sauberer_zustand():
    egress_guard.reset_egress_state()
    agent.request_origin_var.set({})
    yield
    egress_guard.reset_egress_state()


def test_genannte_uploads_findet_dateinamen_in_der_nachricht(tmp_path, monkeypatch):
    from rag_backend import main as m

    ordner = tmp_path / "Uploads"
    ordner.mkdir()
    (ordner / "Kundenreklamation_RK-2025-0042.pdf").write_bytes(b"x")
    (ordner / "preisliste.xlsx").write_bytes(b"x")
    (ordner / "a.md").write_bytes(b"x")   # zu kurz, wuerde ueberall treffen
    monkeypatch.setattr(m, "_UPLOAD_DIR", ordner)

    text = "Beigelegte Datei(en): Uploads/kundenreklamation_rk-2025-0042.PDF. Fasse zusammen, a.md auch."
    assert m._genannte_uploads(text) == ["Kundenreklamation_RK-2025-0042.pdf"]
    assert m._genannte_uploads("Wie wird das Wetter?") == []
    assert m._genannte_uploads("") == []


def test_genannte_uploads_ohne_ordner(tmp_path, monkeypatch):
    from rag_backend import main as m

    monkeypatch.setattr(m, "_UPLOAD_DIR", tmp_path / "fehlt")
    assert m._genannte_uploads("bericht.pdf") == []


def test_beigelegte_datei_macht_das_gespraech_sofort_vertraulich():
    assert not egress_guard.is_conversation_sensitive()
    assert agent.beigelegte_dateien_vermerken({"attached_files": []}) is False
    assert not egress_guard.is_conversation_sensitive()

    assert agent.beigelegte_dateien_vermerken(
        {"attached_files": ["Kundenreklamation_RK-2025-0042.pdf"]}) is True
    assert egress_guard.is_conversation_sensitive()
    assert "upload" in egress_guard.get_current_egress_state().sources


def test_sperrmeldung_wird_nur_am_anfang_erkannt():
    assert egress_guard.is_block_message(egress_guard.BLOCK_MESSAGE)
    assert egress_guard.is_block_message("  " + egress_guard.CONTENT_BLOCK_MESSAGE + " mehr")
    assert not egress_guard.is_block_message("Bericht ... " + egress_guard.BLOCK_MESSAGE)
    assert not egress_guard.is_block_message("")
    assert not egress_guard.is_block_message(None)


def test_relativer_pfad_gilt_ab_dem_arbeitsordner():
    basis = agent._WORKSPACE_CONTAINER.resolve(strict=False)
    assert agent._win_to_container("Uploads/bericht.pdf") == basis / "Uploads" / "bericht.pdf"
    assert agent._win_to_container("Uploads\\bericht.pdf") == basis / "Uploads" / "bericht.pdf"
    #--- Ausbruch und fremde absolute Pfade bleiben draussen.
    assert agent._win_to_container("../etc/passwd") is None
    assert agent._win_to_container("Uploads/../../etc/passwd") is None
    assert agent._win_to_container("/etc/passwd") is None
    assert agent._win_to_container("D:\\Geheim\\x.pdf") is None
    assert agent._win_to_container("\\\\server\\freigabe\\x.pdf") is None


def test_backend_meldet_gesperrte_schritte():
    quelltext = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '{"type": "tool_blocked", "index": _gi}' in quelltext
    assert 'schritte[_gi]["gesperrt"] = True' in quelltext
    agent_text = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert '{"tool_blocked": {"run_id": event.get("run_id")}}' in agent_text


def test_oberflaeche_kennzeichnet_gesperrte_schritte_und_nennt_keinen_windows_pfad():
    voice = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    assert "ev.type === 'tool_blocked'" in voice
    assert "function chatToolGesperrt" in chat
    assert "if (sch.gesperrt) chatToolGesperrt(row);" in chat
    #--- Die Notiz beim Hineinziehen nennt keinen festen Windows-Pfad mehr.
    anfang = chat.index("function chatNotizEinsetzen")
    funktion = chat[anfang:chat.index("\n}\n", anfang)]
    assert "Argus_Workspace" not in funktion
    assert "'Uploads/' + n" in funktion
