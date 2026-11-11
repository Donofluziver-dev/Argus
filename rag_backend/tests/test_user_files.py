"""Tests fuer den persoenlichen Dateibereich je Konto.

Prueft:
- API: Upload, List, Download (attachment + nosniff), Delete
- Authentifizierung, CSRF, Dateiendungen, Quota
- Isolation: Konto A kann weder auf Dateien noch Ergebnisse von Konto B zugreifen
- Traversal- und Symlink-Schutz ohne Server-Pfad-Leck
- Werkzeuge fuer Rolle user: read_document, save_artifact, execute_code
- Sperre nach Firmendaten (attached_files)
- Compose-Abschottung (kein Workspace-Mount in calc-sandbox)
- Oberflaeche (Quelltext-Tests: voice.js, chat.js, keine innerHTML-Leaks)
"""

import base64
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from rag_backend import agent, egress_guard, user_files
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parents[1] / "static"


@pytest.fixture(autouse=True)
def _clean_egress_and_origin():
    egress_guard.reset_egress_state()
    agent.request_origin_var.set({})
    yield
    egress_guard.reset_egress_state()
    agent.request_origin_var.set({})


# ==============================================================================
# 1. API: Upload, List, Download, Delete, CSRF, Auth, Limits
# ==============================================================================


def test_api_upload_list_download_delete_lifecycle(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "admin_user", "einLangesPasswort", "admin")

    # 1. Upload in Dateien
    file_bytes = b"alpha,beta,gamma\n1,2,3\n"
    r = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": ("daten.csv", file_bytes, "text/csv")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["name"] == "daten.csv"
    assert data["bereich"] == "dateien"
    assert data["size"] == len(file_bytes)

    # 2. List
    r_list = client.get("/v1/dashboard/files")
    assert r_list.status_code == 200
    ld = r_list.json()
    assert len(ld["dateien"]) == 1
    assert ld["dateien"][0]["name"] == "daten.csv"
    assert ld["used_bytes"] == len(file_bytes)
    assert ld["max_bytes"] > 0

    # 3. Download
    r_dl = client.get("/v1/dashboard/files/dateien/daten.csv")
    assert r_dl.status_code == 200
    assert r_dl.headers.get("content-type") == "application/octet-stream"
    assert "attachment" in r_dl.headers.get("content-disposition", "")
    assert r_dl.headers.get("x-content-type-options") == "nosniff"
    assert r_dl.content == file_bytes

    # 4. Durchnummerierung bei zweitem Upload gleichen Namens
    r_dup = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": ("daten.csv", b"zeile2\n", "text/csv")},
    )
    assert r_dup.status_code == 200
    assert r_dup.json()["name"] == "daten_2.csv"

    # 5. Delete
    r_del = client.delete("/v1/dashboard/files/dateien/daten.csv", headers=DASH_ORIGIN)
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "deleted"

    # Danach nicht mehr lesbar
    r_dl_after = client.get("/v1/dashboard/files/dateien/daten.csv")
    assert r_dl_after.status_code == 404


def test_api_auth_and_csrf_checks(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))

    # Unangemeldet: 401
    assert client.get("/v1/dashboard/files").status_code == 401
    assert client.post("/v1/dashboard/files", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 401

    # Angemeldet
    _make_account(client, "admin_user", "einLangesPasswort", "admin")

    # POST ohne Origin: 403
    r_no_origin = client.post("/v1/dashboard/files", files={"file": ("a.txt", b"x", "text/plain")})
    assert r_no_origin.status_code == 403

    # DELETE ohne Origin: 403
    r_del_no_origin = client.delete("/v1/dashboard/files/dateien/a.txt")
    assert r_del_no_origin.status_code == 403


def test_api_disallowed_extension_rejected(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "admin_user", "einLangesPasswort", "admin")

    r = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": ("secret.exe", b"MZ...", "application/octet-stream")},
    )
    assert r.status_code == 415
    assert "nicht zugelassen" in r.text


def test_api_quota_limit_enforced(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("USER_FILES_MAX_MB", "1")  # 1 MB Limit
    _make_account(client, "admin_user", "einLangesPasswort", "admin")

    # 2 MB Datei hochladen -> muss abgelehnt werden (413)
    big_chunk = b"X" * (2 * 1024 * 1024)
    r = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": ("gross.txt", big_chunk, "text/plain")},
    )
    assert r.status_code == 413
    assert "Speicherplatzgrenze" in r.text or "groesser als" in r.text


# ==============================================================================
# 2. Isolation: Konto A vs. Konto B, Traversal, Symlinks
# ==============================================================================


def test_isolation_between_two_accounts(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))

    # Admin anlegen
    _make_account(client, "chef", "einLangesPasswort", "admin")

    # Nutzer A und Nutzer B anlegen
    r_a = client.post(
        "/v1/dashboard/users",
        headers=DASH_ORIGIN,
        json={"username": "user_a", "password": "einLangesPasswort", "role": "user"},
    )
    assert r_a.status_code == 200, r_a.text
    r_b = client.post(
        "/v1/dashboard/users",
        headers=DASH_ORIGIN,
        json={"username": "user_b", "password": "einLangesPasswort", "role": "user"},
    )
    assert r_b.status_code == 200, r_b.text

    # Login als Nutzer A
    client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                json={"username": "user_a", "password": "einLangesPasswort"})

    # Nutzer A laedt vertrauliche Datei hoch
    r_up = client.post(
        "/v1/dashboard/files",
        headers=DASH_ORIGIN,
        files={"file": ("vertraulich_a.pdf", b"Geheimnis A", "application/pdf")},
    )
    assert r_up.status_code == 200

    # Login als Nutzer B
    client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                json={"username": "user_b", "password": "einLangesPasswort"})

    # Nutzer B sieht Datei von A NICHT in der Liste
    r_list = client.get("/v1/dashboard/files")
    assert r_list.status_code == 200
    b_files = [f["name"] for f in r_list.json()["dateien"]]
    assert "vertraulich_a.pdf" not in b_files

    # Nutzer B kann Datei von A NICHT herunterladen (404)
    r_dl = client.get("/v1/dashboard/files/dateien/vertraulich_a.pdf")
    assert r_dl.status_code == 404

    # Nutzer B kann Datei von A NICHT loeschen (404)
    r_del = client.delete("/v1/dashboard/files/dateien/vertraulich_a.pdf", headers=DASH_ORIGIN)
    assert r_del.status_code == 404


def test_traversal_attacks_rejected_without_server_path_leak(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "chef", "einLangesPasswort", "admin")

    # Direkte Aufrufe von safe_filename und resolve_user_file mit Traversals
    for bad_name in ["..", ".", "../etc/passwd", "..\\boot.ini", "%2e%2e", "", "   "]:
        with pytest.raises(ValueError):
            user_files.safe_filename(bad_name)
        with pytest.raises(ValueError):
            user_files.resolve_user_file(1, "dateien", bad_name)

    attacks = [
        "../etc/passwd",
        "..\\windows\\win.ini",
        "%2e%2e",
        "%2e",
        "%2e%2e/probe.txt",
        "/host/argus_workspace/Uploads/x",
        "C:\\Argus_Workspace\\Uploads\\x",
        "dateien:stream",
        " ",
    ]

    for att in attacks:
        r = client.get(f"/v1/dashboard/files/dateien/{att}")
        assert r.status_code in (400, 404), (att, r.status_code)
        text = r.text
        # Niemals duerfen Server-Pfade nach aussen dringen
        assert "/host/argus_workspace" not in text
        assert "C:\\Argus_Workspace" not in text
        assert str(tmp_path) not in text


def test_symlink_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))

    outside_file = tmp_path / "host_secret.txt"
    outside_file.write_text("GEHEIMNIS_AUSSERHALB", encoding="utf-8")

    user_dateien = tmp_path / "users" / "42" / "Dateien"
    user_dateien.mkdir(parents=True)
    symlink_file = user_dateien / "leak_link.txt"

    # Auch ein Symlink, der innerhalb des Bereichs liegt
    inside_target = user_dateien / "inside_target.txt"
    inside_target.write_text("INSIDE", encoding="utf-8")
    inside_symlink = user_dateien / "inside_link.txt"

    try:
        os.symlink(str(outside_file), str(symlink_file))
        os.symlink(str(inside_target), str(inside_symlink))
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks auf diesem Dateisystem/OS nicht unterstuetzt.")

    # Symlink nach draussen -> abgelehnt
    with pytest.raises(ValueError, match="Symlinks sind nicht erlaubt"):
        user_files.resolve_user_file(42, "dateien", "leak_link.txt", must_exist=True)

    # Symlink innerhalb des Bereichs -> ebenfalls abgelehnt
    with pytest.raises(ValueError, match="Symlinks sind nicht erlaubt"):
        user_files.resolve_user_file(42, "dateien", "inside_link.txt", must_exist=True)


# ==============================================================================
# 3. Werkzeuge (agent.py): read_document, save_artifact, execute_code
# ==============================================================================


def test_user_role_has_user_files_tools_but_no_admin_tools():
    offered = {t.name for t in agent._tools_for_role(list(agent.tools), "user")}
    # Freigegeben
    assert {"read_document", "save_artifact", "execute_code"} <= offered
    # Admin-only
    assert "ingest_document" not in offered
    assert "index_document" not in offered
    assert "delete_indexed_document" not in offered
    assert not any(n.startswith("fs_") for n in offered)
    assert not offered & agent._TOOL_GROUP_NAMES["cloud"]
    assert not offered & agent._TOOL_GROUP_NAMES["agents"]


@pytest.mark.asyncio
async def test_read_document_as_user(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    # Datei im eigenen Bereich anlegen
    user_dir = tmp_path / "users" / "10" / "Dateien"
    user_dir.mkdir(parents=True)
    (user_dir / "bericht.txt").write_text("Das ist der vertrauliche Bericht.", encoding="utf-8")

    # Als User 10
    agent.request_origin_var.set({"user_id": 10, "user_role": "user"})

    # 1. 'Dateien/bericht.txt' -> erfolgreich
    res = await agent.read_document.ainvoke({"path": "Dateien/bericht.txt"})
    assert "Das ist der vertrauliche Bericht." in res

    # 2. Blosser Dateiname 'bericht.txt' -> erfolgreich (heisst Dateien/bericht.txt)
    res_bare = await agent.read_document.ainvoke({"path": "bericht.txt"})
    assert "Das ist der vertrauliche Bericht." in res_bare

    # 3. Ausbruchsversuch '../2/Dateien/x' -> abgewiesen ohne Pfad-Details
    res_esc = await agent.read_document.ainvoke({"path": "../2/Dateien/x"})
    assert res_esc.startswith("ERROR:")
    assert "C:\\" not in res_esc
    assert "/host/" not in res_esc

    # 4. Host-Workspace-Pfad als User -> abgewiesen
    res_ws = await agent.read_document.ainvoke({"path": "/host/argus_workspace/Uploads/x"})
    assert res_ws.startswith("ERROR:")
    assert "/host/" not in res_ws

    res_win = await agent.read_document.ainvoke({"path": "C:\\Argus_Workspace\\Uploads\\x"})
    assert res_win.startswith("ERROR:")


@pytest.mark.asyncio
async def test_save_artifact_saves_to_ergebnisse_without_windows_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    agent.request_origin_var.set({"user_id": 15, "user_role": "user"})
    agent.dateien_zuruecksetzen()

    res = await agent.save_artifact.ainvoke({
        "filename": "analyse.md",
        "content": "# Analyseergebnis\nAlles im gruenen Bereich.",
    })

    assert res == "Saved as Ergebnisse/analyse.md; the user can download it from the chat."
    assert "C:\\" not in res

    target = tmp_path / "users" / "15" / "Ergebnisse" / "analyse.md"
    assert target.is_file()
    assert "# Analyseergebnis" in target.read_text(encoding="utf-8")

    # SSE-Reporting: Datei muss gemerkt sein
    gemerkt = agent.dateien_liste()
    assert len(gemerkt) == 1
    assert gemerkt[0]["name"] == "analyse.md"
    assert gemerkt[0]["bereich"] == "ergebnisse"


@pytest.mark.asyncio
async def test_execute_code_sends_user_files_and_saves_results(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    async def _noop():
        pass
    monkeypatch.setattr(agent, "_ensure_models_loaded", _noop)

    # Datei im Ordner Dateien anlegen
    user_d = tmp_path / "users" / "20" / "Dateien"
    user_d.mkdir(parents=True)
    (user_d / "tabelle.csv").write_bytes(b"A,B\n1,2\n")

    agent.request_origin_var.set({"user_id": 20, "user_role": "user"})
    agent.dateien_zuruecksetzen()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "result": "Daten verarbeitet: 1 Zeile.",
        "files": [
            {
                "name": "auswertung.csv",
                "content": base64.b64encode(b"Ergebnisdaten").decode("ascii"),
            }
        ],
    }

    sent_requests = []

    async def fake_post(url, **kwargs):
        sent_requests.append(kwargs)
        return mock_resp

    monkeypatch.setattr(agent, "_http_client_internal", MagicMock(post=AsyncMock(side_effect=fake_post)))

    out = await agent.execute_code.ainvoke({"code": "print('start')"})

    # 1. Geprueft: Es wurden genau die Dateien aus dem eigenen Bereich mitgeschickt
    assert len(sent_requests) == 1
    payload = sent_requests[0]["json"]
    assert payload["language"] == "python"
    sent_files = payload.get("files", [])
    assert len(sent_files) == 1
    assert sent_files[0]["name"] == "tabelle.csv"
    assert base64.b64decode(sent_files[0]["content"]) == b"A,B\n1,2\n"

    # 2. Geprueft: Rueckgabe enthaelt Zeile 'Files created: Ergebnisse/auswertung.csv'
    assert "Files created: Ergebnisse/auswertung.csv" in out

    # 3. Geprueft: Datei landet in Ergebnisse
    erg_file = tmp_path / "users" / "20" / "Ergebnisse" / "auswertung.csv"
    assert erg_file.is_file()
    assert erg_file.read_bytes() == b"Ergebnisdaten"


# ==============================================================================
# 4. Sperre nach Firmendaten: _genannte_uploads
# ==============================================================================


def test_genannte_uploads_for_all_roles(tmp_path, monkeypatch):
    from rag_backend import main as m

    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    # Eigenes Verzeichnis fuer User 30
    u30_dir = tmp_path / "users" / "30" / "Dateien"
    u30_dir.mkdir(parents=True)
    (u30_dir / "Eigene_Rechnung_2026.pdf").write_bytes(b"x")

    # Verzeichnis fuer fremden User 99
    u99_dir = tmp_path / "users" / "99" / "Dateien"
    u99_dir.mkdir(parents=True)
    (u99_dir / "Fremde_Akte_Geheim.pdf").write_bytes(b"x")

    # Gemeinsames Uploads-Verzeichnis
    shared_dir = tmp_path / "Uploads"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Gemeinsamer_Vertrag.pdf").write_bytes(b"x")
    monkeypatch.setattr(m, "_UPLOAD_DIR", shared_dir)

    # 1. Rolle 'user' nennt eigene Datei -> gefunden
    text_eigen = "Bitte pruefe Dateien/eigene_rechnung_2026.pdf fuer mich."
    res_user = m._genannte_uploads(text_eigen, user_id=30, role="user")
    assert res_user == ["Eigene_Rechnung_2026.pdf"]

    # 2. Rolle 'user' nennt fremde Datei -> NICHT gefunden
    text_fremd = "Kannst du Fremde_Akte_Geheim.pdf oeffnen?"
    assert m._genannte_uploads(text_fremd, user_id=30, role="user") == []

    # 3. Rolle 'user' nennt gemeinsame Uploads-Datei -> fuer User nicht erreichbar
    text_shared = "Lies Gemeinsamer_Vertrag.pdf"
    assert m._genannte_uploads(text_shared, user_id=30, role="user") == []

    # 4. Rolle 'admin' nennt gemeinsame Uploads-Datei -> gefunden
    assert m._genannte_uploads(text_shared, user_id=30, role="admin") == ["Gemeinsamer_Vertrag.pdf"]


# ==============================================================================
# 5. Compose-Abschottung
# ==============================================================================


def test_calc_sandbox_has_no_workspace_volume_mount():
    compose_path = ROOT / "docker-compose.yml"
    assert compose_path.is_file()
    content = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    services = content.get("services", {})
    assert "calc-sandbox" in services
    sandbox = services["calc-sandbox"]

    # Darf keine Volumes auf den Workspace haben
    volumes = sandbox.get("volumes", [])
    assert not volumes or len(volumes) == 0
    for v in volumes:
        assert "ARGUS_WORKSPACE" not in str(v)
        assert "/app/data" not in str(v)

    # tmpfs muss size=256m haben
    tmpfs = sandbox.get("tmpfs", [])
    assert any("size=256m" in str(entry) and "noexec" in str(entry) for entry in tmpfs)


# ==============================================================================
# 6. Oberflaeche: Quelltext-Pruefungen (voice.js, chat.js)
# ==============================================================================


def test_oberflaeche_handles_files_event_and_pills_without_innerhtml():
    voice = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")

    # voice.js verarbeitet files-Event
    assert "ev.type === 'files'" in voice
    assert "cb.onFiles" in voice

    # chat.js reagiert auf onFiles
    assert "onFiles: function" in chat

    # chatApplySteps baut steps.dateien auf
    assert "chatAddResultFiles(msg, steps.dateien || [])" in chat

    # chatAddResultFiles nutzt Download-Links und kein innerHTML mit Dateinamen
    anfang = chat.index("function chatAddResultFiles")
    ende = chat.index("\n}\n", anfang)
    funktion = chat[anfang:ende]
    assert "innerHTML" not in funktion
    assert "textContent" in funktion
    assert "encodeURIComponent(f.name)" in funktion
    assert "setAttribute('download'" in funktion

    # Notiz beim Hineinziehen nutzt 'Dateien/'
    anfang_notiz = chat.index("function chatNotizEinsetzen")
    ende_notiz = chat.index("\n}\n", anfang_notiz)
    funktion_notiz = chat[anfang_notiz:ende_notiz]
    assert "'Dateien/' + n" in funktion_notiz


# ==============================================================================
# 8. Weitere Pruefungen: Fallbacks, Admin-Isolation, MCP-Filesystem, Error-Handling
# ==============================================================================


@pytest.mark.asyncio
async def test_save_artifact_without_user_account_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    # Ohne user_id im Kontext
    agent.request_origin_var.set({})
    res1 = await agent.save_artifact.ainvoke({"filename": "report.md", "content": "test"})
    assert res1 == "ERROR: No user account for this request; nothing was saved."

    # Mit ungueltiger user_id
    agent.request_origin_var.set({"user_id": 0})
    res2 = await agent.save_artifact.ainvoke({"filename": "report.md", "content": "test"})
    assert res2 == "ERROR: No user account for this request; nothing was saved."

    # Es darf kein Ordner oder Datei unter users/ angelegt worden sein
    users_dir = tmp_path / "users"
    if users_dir.exists():
        assert not any(users_dir.rglob("*.md"))


@pytest.mark.asyncio
async def test_admin_cannot_access_other_user_files_via_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(agent, "_WORKSPACE_CONTAINER", tmp_path)
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    # Fremde Datei von User 2
    u2_file = tmp_path / "users" / "2" / "Dateien" / "vertraulich.txt"
    u2_file.parent.mkdir(parents=True)
    u2_file.write_text("Fremde Geheimnisse von User 2", encoding="utf-8")

    # Eigene Datei von Admin (User 1)
    u1_file = tmp_path / "users" / "1" / "Dateien" / "admin_notiz.txt"
    u1_file.parent.mkdir(parents=True)
    u1_file.write_text("Admin eigene Datei", encoding="utf-8")

    # Als Admin angemeldet
    agent.request_origin_var.set({"user_role": "admin", "user_id": 1})

    # 1. read_document ueber Windows-Workspace-Pfad auf fremden Bereich -> abgelehnt
    res_win = await agent.read_document.ainvoke({"path": "C:\\Argus_Workspace\\users\\2\\Dateien\\vertraulich.txt"})
    assert "outside" in res_win or "denied" in res_win or "ERROR" in res_win
    assert "Fremde Geheimnisse" not in res_win

    # 2. read_document ueber Container-Workspace-Pfad auf fremden Bereich -> abgelehnt
    res_cont = await agent.read_document.ainvoke({"path": str(u2_file)})
    assert "outside" in res_cont or "denied" in res_cont or "ERROR" in res_cont
    assert "Fremde Geheimnisse" not in res_cont

    # 3. ingest_document ueber Windows-Pfad -> abgelehnt
    res_ing_win = await agent.ingest_document.ainvoke({"path": "C:\\Argus_Workspace\\users\\2\\Dateien\\vertraulich.txt"})
    assert "outside" in res_ing_win or "ERROR" in res_ing_win

    # 4. ingest_document ueber Container-Pfad -> abgelehnt
    res_ing_cont = await agent.ingest_document.ainvoke({"path": str(u2_file)})
    assert "outside" in res_ing_cont or "ERROR" in res_ing_cont

    # 5. Eigener Bereich ueber 'Dateien/...' bleibt fuer Admin normal lesbar
    res_own = await agent.read_document.ainvoke({"path": "Dateien/admin_notiz.txt"})
    assert "Admin eigene Datei" in res_own


def test_fs_mcp_blocks_user_dateien_and_ergebnisse_but_allows_memory(tmp_path, monkeypatch):
    from rag_backend.mcp_servers import filesystem as fs

    monkeypatch.setattr(fs, "WORKSPACE_CONTAINER", tmp_path)

    # Dateien und Ergebnisse von User 2 anlegen
    u2_dateien = tmp_path / "users" / "2" / "Dateien" / "geheim.txt"
    u2_dateien.parent.mkdir(parents=True)
    u2_dateien.write_text("Streng geheim", encoding="utf-8")

    u2_ergebnisse = tmp_path / "users" / "2" / "Ergebnisse" / "artefakt.txt"
    u2_ergebnisse.parent.mkdir(parents=True)
    u2_ergebnisse.write_text("Ergebnis geheim", encoding="utf-8")

    # Gedaechtnis von User 2 anlegen
    u2_mem = tmp_path / "users" / "2" / "MEMORY.md"
    u2_mem.write_text("Langzeitgedaechtnis User 2", encoding="utf-8")

    # Lesen in Dateien/Ergebnisse verboten
    with pytest.raises(PermissionError):
        fs.fs_read_file(str(u2_dateien))
    with pytest.raises(PermissionError):
        fs.fs_read_file("users/2/Dateien/geheim.txt")
    with pytest.raises(PermissionError):
        fs.fs_read_file("users/2/Ergebnisse/artefakt.txt")

    # Schreiben in Dateien/Ergebnisse verboten
    with pytest.raises(PermissionError):
        fs.fs_write_file("users/2/Dateien/hacked.txt", "bad")
    with pytest.raises(PermissionError):
        fs.fs_write_file("users/2/Ergebnisse/hacked.txt", "bad")
    with pytest.raises(PermissionError):
        fs.fs_mkdir("users/2/Dateien/subdir")

    # Gedaechtnis bleibt erlaubt (wie bisher)
    read_mem = fs.fs_read_file("users/2/MEMORY.md")
    assert "Langzeitgedaechtnis User 2" in read_mem
    write_res = fs.fs_write_file("users/2/MEMORY.md", "Aktualisiertes Gedaechtnis")
    assert "OK written" in write_res
    assert u2_mem.read_text(encoding="utf-8") == "Aktualisiertes Gedaechtnis"


def test_coll_upload_button_in_index_html_opens_argus_files():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    # Knopf "HOCHLADEN" existiert in der COLLECTION DOCS Karte vor coll-ingest
    assert 'id="coll-upload"' in html
    assert "HOCHLADEN</button>" in html
    #--- Der Klick haengt in ops.js, nicht als onclick im HTML.
    ops = (STATIC / "js" / "ops.js").read_text(encoding="utf-8")
    assert '$("coll-upload")' in ops and "argusFiles.open()" in ops

    # Kommentar ueber files-modal angepasst
    assert "Geoeffnet aus dem Monitor, nur fuer Administratoren" in html


@pytest.mark.asyncio
async def test_read_document_error_message_non_admin_does_not_leak_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)

    u5_dir = tmp_path / "users" / "5" / "Dateien"
    u5_dir.mkdir(parents=True)
    doc_path = u5_dir / "crash.pdf"
    doc_path.write_bytes(b"dummy pdf bytes")

    agent.request_origin_var.set({"user_role": "user", "user_id": 5})

    # Simuliere Exception im load_file mit internen Serverpfaden
    from rag_backend import ingest
    def fake_load_file(p):
        raise RuntimeError("Internal parser crash at /var/secrets/internal/kernel_map.py line 42")

    monkeypatch.setattr(ingest, "load_file", fake_load_file)

    res = await agent.read_document.ainvoke({"path": "Dateien/crash.pdf"})
    assert res == "ERROR: Failed to read document."
    assert "/var/secrets" not in res
