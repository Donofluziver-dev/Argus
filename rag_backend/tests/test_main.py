import os

#--- Test-Umgebung VOR jedem rag_backend-Import setzen. Steht sie darunter,
#--- hat der Import crypto_utils bereits mit dem dann geltenden Fernet-Key
#--- initialisiert.
os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
#--- 32+ Zeichen: darunter warnt PyJWT bei jedem encode/decode (HS256).
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from rag_backend.models import Base
from rag_backend.main import app, get_db
from rag_backend.ingest import ingest
from pathlib import Path
import time
from unittest.mock import patch, MagicMock
import asyncio

SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Lifespan neutralisieren: der echte Startup laeuft gegen die
# postgres-gebundene SessionLocal, die der Host nicht aufloesen kann.
from contextlib import asynccontextmanager

@asynccontextmanager
async def _noop_lifespan(_app):
    yield

app.router.lifespan_context = _noop_lifespan

#--- Rate-Limits in den Tests aus: alle Anfragen kommen von derselben
#--- Adresse und wuerden sich gegenseitig aussperren.
from rag_backend.main import limiter as _limiter

_limiter.enabled = False

@pytest.fixture(name="db_session", scope="function")
def override_get_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        db_file = Path("test.db")
        if db_file.exists():
            db_file.unlink()

@pytest.fixture(name="client", scope="function")
def test_client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    #--- base_url ist PFLICHT wegen des Host-Header-Gates: TestClient
    #--- schickt sonst 'Host: testserver' und bekommt 400. 'testserver'
    #--- gehoert NICHT in TRUSTED_HOSTS.
    with TestClient(app, base_url="http://127.0.0.1:7860") as client:
        yield client
    app.dependency_overrides.clear()

@pytest.fixture(name="ingest_in_memory", scope="session")
def ingest_in_memory_fixture():
    collection_name = "test_collection_" + str(time.time()).replace(".", "")
    os.environ["QDRANT_IN_MEMORY"] = "true"
    os.environ["QDRANT_COLLECTION"] = collection_name
    test_docs_dir = Path("test_documents_to_ingest")
    test_docs_dir.mkdir(exist_ok=True)
    (test_docs_dir / "test.txt").write_text("Dies ist ein Test.")
    ingest(data_path=str(test_docs_dir))
    yield collection_name
    (test_docs_dir / "test.txt").unlink()
    test_docs_dir.rmdir()
    del os.environ["QDRANT_IN_MEMORY"]
    del os.environ["QDRANT_COLLECTION"]

def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

#--- Dashboard-Zugang: Anmeldung, Rollen, Upload-Grenzen ---------------------
#--- Das Netz unter der Rollentrennung: faellt an einer Route das Gate weg,
#--- faellt es hier auf.

DASH_ORIGIN = {"Origin": "http://127.0.0.1:7860"}

#--- Wer was sehen darf. Bewusst redundant zu den Depends() in main.py.
ADMIN_ONLY_GET = [
    "/v1/dashboard/summary", "/v1/dashboard/audit", "/v1/dashboard/state",
    "/v1/dashboard/documents", "/v1/dashboard/missions", "/v1/dashboard/evals",
    "/v1/dashboard/eval_logs", "/v1/dashboard/uploads",
    "/v1/dashboard/users", "/v1/dashboard/confirmations",
]
ANY_ACCOUNT_GET = ["/v1/dashboard/config", "/v1/dashboard/chats"]


def _make_account(client, username, password, role):
    '''Legt ein Konto an und liefert einen angemeldeten Client-Cookie-Satz.'''
    from rag_backend.main import _login_accounts
    from rag_backend.database import get_db as _unused  # noqa: F401
    r = client.get("/v1/dashboard/auth/state")
    if r.json().get("needs_setup"):
        r = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                        json={"username": username, "password": password, "role": "admin"})
        assert r.status_code == 200, r.text
        return r
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_dashboard_requires_login(client):
    for path in ADMIN_ONLY_GET + ANY_ACCOUNT_GET:
        assert client.get(path).status_code == 401, path


def test_first_account_becomes_admin(client):
    r = client.get("/v1/dashboard/auth/state")
    assert r.json()["needs_setup"] is True
    r = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                    json={"username": "chef", "password": "einLangesPasswort", "role": "admin"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
    # Zweiter Erstlauf muss zu sein, sonst koennte sich jeder zum Admin machen.
    r2 = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                     json={"username": "zweiter", "password": "einLangesPasswort", "role": "admin"})
    assert r2.status_code == 409


def test_self_registration_never_yields_admin(client, monkeypatch):
    '''Der Kern der Selbstregistrierung: die Rolle steht serverseitig fest.

    Geprueft wird auch der Versuch, sie ueber den Body zu setzen -- genau daran
    scheitert der alte /register-Endpunkt, dessen Spalten-Default "admin" ist.'''
    #--- Selbstregistrierung ist per Default aus; hier ausdruecklich an.
    monkeypatch.setenv("DASHBOARD_ALLOW_REGISTER", "true")
    _make_account(client, "chef", "einLangesPasswort", "admin")
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)

    r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                    json={"username": "neuling", "password": "einLangesPasswort"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"

    # Rolle im Body wird ignoriert, nicht uebernommen.
    r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                    json={"username": "schlaufuchs", "password": "einLangesPasswort",
                          "role": "admin"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"

    # Und das frisch registrierte Konto kommt an keine Betriebsdaten.
    for path in ADMIN_ONLY_GET:
        assert client.get(path).status_code == 403, path


def test_self_registration_requires_first_admin(client, monkeypatch):
    #--- Selbstregistrierung ist per Default aus; hier ausdruecklich an.
    monkeypatch.setenv("DASHBOARD_ALLOW_REGISTER", "true")
    # Vor dem ersten Konto fuehrt der Weg ueber /auth/setup.
    assert client.get("/v1/dashboard/auth/state").json()["needs_setup"] is True
    r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                    json={"username": "voreilig", "password": "einLangesPasswort"})
    assert r.status_code == 409


def test_self_registration_can_be_switched_off(client, monkeypatch):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    monkeypatch.setenv("DASHBOARD_ALLOW_REGISTER", "false")
    assert client.get("/v1/dashboard/auth/state").json()["allow_register"] is False
    r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                    json={"username": "zuspaet", "password": "einLangesPasswort"})
    assert r.status_code == 403


def test_chat_role_is_locked_out_of_operations(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                    json={"username": "phoenix", "password": "einLangesPasswort", "role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"

    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                    json={"username": "phoenix", "password": "einLangesPasswort"})
    assert r.status_code == 200 and r.json()["role"] == "user"

    for path in ADMIN_ONLY_GET:
        assert client.get(path).status_code == 403, path
    for path in ANY_ACCOUNT_GET:
        assert client.get(path).status_code == 200, path
    # Ein 'chat'-Konto darf sich auch nicht selbst befoerdern.
    r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                    json={"username": "hintertuer", "password": "einLangesPasswort", "role": "admin"})
    assert r.status_code == 403


def test_chats_are_separated_per_account(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    chat_id = client.post("/v1/dashboard/chats", headers=DASH_ORIGIN).json()["id"]
    client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                json={"username": "phoenix", "password": "einLangesPasswort", "role": "user"})
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                json={"username": "phoenix", "password": "einLangesPasswort"})
    # Fremder Verlauf: 404 statt 403, damit die Existenz nicht durchsickert.
    assert client.get("/v1/dashboard/chats/" + str(chat_id)).status_code == 404
    assert client.delete("/v1/dashboard/chats/" + str(chat_id),
                         headers=DASH_ORIGIN).status_code == 404
    assert client.get("/v1/dashboard/chats").json()["chats"] == []


def test_dashboard_csrf_blocks_foreign_origin(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    assert client.post("/v1/dashboard/chats",
                       headers={"Origin": "http://boese.example"}).status_code == 403
    # Ohne Origin/Referer: kein Browser-Kontext, also abgelehnt.
    assert client.post("/v1/dashboard/chats").status_code == 403


def test_last_admin_cannot_be_demoted(client):
    r = _make_account(client, "chef", "einLangesPasswort", "admin")
    uid = r.json()["user_id"]
    r = client.post("/v1/dashboard/users/" + str(uid) + "/role?role=user",
                    headers=DASH_ORIGIN)
    assert r.status_code == 409


def test_upload_name_is_sanitized():
    from rag_backend.main import _safe_upload_name
    import pytest as _pytest
    from fastapi import HTTPException

    assert _safe_upload_name("../../../etc/passwd.txt") == "passwd.txt"
    assert _safe_upload_name("..\\..\\windows\\notiz.txt") == "notiz.txt"
    # Doppelpunkt = alternativer NTFS-Datenstrom, muss entschaerft werden.
    assert ":" not in _safe_upload_name("bericht.txt:geheim")
    for evil in ["", "..", "...", "/", "\\"]:
        with _pytest.raises(HTTPException):
            _safe_upload_name(evil)


def test_upload_path_stays_in_workspace():
    from rag_backend.main import _upload_path, _UPLOAD_DIR
    import pytest as _pytest
    from fastapi import HTTPException

    assert _upload_path("bericht.pdf").parent == _UPLOAD_DIR.resolve(strict=False)
    for evil in ["../../run/secrets/ssh_key", "..\\..\\secrets"]:
        # Entweder der Name wird flachgeklopft oder die Pruefung schlaegt zu
        # -- ein Pfad AUSSERHALB darf nie entstehen.
        try:
            p = _upload_path(evil)
        except HTTPException:
            continue
        assert p.parent == _UPLOAD_DIR.resolve(strict=False), evil

def test_ingest_all_nimmt_nur_lesbare_dateien(client, tmp_path, monkeypatch):
    #--- Bilder liegen im selben Ordner, tragen aber keinen Text. Sie
    #--- duerfen den Sammellauf weder abbrechen noch als Fehler auftauchen.
    from rag_backend import main as m
    import rag_backend.agent as ag

    _make_account(client, "chef", "einLangesPasswort", "admin")

    ordner = tmp_path / "Uploads"
    ordner.mkdir()
    (ordner / "notiz.txt").write_text("Inhalt fuer den Index.", encoding="utf-8")
    (ordner / "bild.png").write_bytes(b"kein Text")
    monkeypatch.setattr(m, "_UPLOAD_DIR", ordner)

    gesehen = []

    async def _fake_store(name, text, source=None):
        gesehen.append(name)
        return "OK"

    monkeypatch.setattr(ag, "_store_text_in_qdrant", _fake_store)

    r = client.post("/v1/dashboard/uploads/ingest_all", headers=DASH_ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["verarbeitet"] == 1
    assert r.json()["indexiert"] == 1
    assert gesehen == ["notiz.txt"]


def test_ingest_all_braucht_admin_und_csrf(client, tmp_path, monkeypatch):
    from rag_backend import main as m

    #--- Ohne Anmeldung: kein Zugang.
    assert client.post("/v1/dashboard/uploads/ingest_all").status_code in (401, 403)

    _make_account(client, "chef", "einLangesPasswort", "admin")
    ordner = tmp_path / "Uploads"
    ordner.mkdir()
    monkeypatch.setattr(m, "_UPLOAD_DIR", ordner)
    #--- Angemeldet, aber ohne Origin: kein Browser-Kontext -> CSRF-Sperre.
    assert client.post("/v1/dashboard/uploads/ingest_all").status_code == 403
    r = client.post("/v1/dashboard/uploads/ingest_all", headers=DASH_ORIGIN)
    assert r.status_code == 200 and r.json()["verarbeitet"] == 0


@pytest.mark.asyncio
@patch("rag_backend.agent._http_client_web", MagicMock())
@patch("rag_backend.agent._http_client_internal", MagicMock())
@patch("rag_backend.agent._qdrant_client")
async def test_document_search_xml_format(mock_qdrant, monkeypatch):
    from rag_backend import retrieval
    from rag_backend.agent import document_search

    async def _embed(client, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def _rerank(client, query, docs):
        return [0.95 for _ in docs]

    monkeypatch.setattr(retrieval, "embed", _embed)
    monkeypatch.setattr(retrieval, "rerank", _rerank)

    mock_hit = MagicMock()
    mock_hit.payload = {"text": "Testinhalt aus Qdrant.", "original_filename": "test_doc.txt"}

    mock_query_response = MagicMock()
    mock_query_response.points = [mock_hit]
    mock_qdrant.query_points.return_value = mock_query_response

    result = await document_search.ainvoke({"query": "Test"})

    assert "<document index=\"1\"" in result
    assert "source=\"test_doc.txt\"" in result
    assert "relevance=\"0.95\"" in result
    assert "Testinhalt aus Qdrant." in result

def test_sanitize_tool_args_strips_sglang_leak():
    # SGLang laesst bei grammar-constrained Decoding das schliessende '>'
    # von '</parameter>' weg -> das Fragment leakt ans Wert-Ende.
    from rag_backend.agent import _sanitize_tool_args
    leaked = {
        'goal': 'Marktanalyse zu KI-Agenten\n</parameter',
        'context': 'fuer eine Praesentation\n</parameter',
        'provider': 'gemini\n</parameter',
    }
    clean = _sanitize_tool_args(leaked)
    assert clean['goal'] == 'Marktanalyse zu KI-Agenten'
    assert clean['context'] == 'fuer eine Praesentation'
    assert clean['provider'] == 'gemini'
    # Gestapelte Fragmente (letzter Parameter vor </function></tool_call>)
    assert _sanitize_tool_args({'q': 'x\n</parameter\n</function\n</tool_call'})['q'] == 'x'
    # KEINE Beschaedigung legitimer Werte
    assert _sanitize_tool_args({'a': 'gemini'})['a'] == 'gemini'
    assert _sanitize_tool_args({'a': '3 < 5 ist wahr'})['a'] == '3 < 5 ist wahr'
    assert _sanitize_tool_args({'a': 'a<b und x>y'})['a'] == 'a<b und x>y'
    # Geschlossenes Tag mitten im Text bleibt unberuehrt
    assert _sanitize_tool_args({'a': 'Nutze </parameter> als Beispiel hier'})['a'] == 'Nutze </parameter> als Beispiel hier'
    # Rekursiv in verschachtelten Strukturen
    nested = _sanitize_tool_args({'outer': {'inner': 'v\n</parameter'}, 'lst': ['a\n</parameter', 'b']})
    assert nested['outer']['inner'] == 'v'
    assert nested['lst'] == ['a', 'b']
    # Zweite Leak-Variante (10.07.): '</parameter >' MIT Leerzeichen vor '>'
    assert _sanitize_tool_args({'q': 'wert\n</parameter >'})['q'] == 'wert'
    # Leak plus fehlserialisierte Liste -> JSON-Koerzion repariert den Typ.
    coerced = _sanitize_tool_args({'subtopics': '["a", "b"]\n</parameter >'})
    assert coerced['subtopics'] == ['a', 'b']
    # OHNE Leak bleibt ein JSON-artiger String bewusst String (execute_code!)
    assert _sanitize_tool_args({'code': '[1, 2, 3]'})['code'] == '[1, 2, 3]'
    # Leak + ungueltiges JSON -> gestrippter String, keine Koerzion
    assert _sanitize_tool_args({'a': '[PERSON_1]\n</parameter'})['a'] == '[PERSON_1]'

def test_sanitize_via_aimessage_patch():
    # Der Monkeypatch muss den Leak bereits bei AIMessage-Konstruktion entfernen.
    from langchain_core.messages import AIMessage
    msg = AIMessage(content='', tool_calls=[{
        'name': 'start_mission',
        'args': {'goal': 'Recherche\n</parameter', 'provider': 'gemini\n</parameter'},
        'id': 'call_1', 'type': 'tool_call',
    }])
    assert msg.tool_calls[0]['args']['goal'] == 'Recherche'
    assert msg.tool_calls[0]['args']['provider'] == 'gemini'

def test_tool_call_rescue_from_reasoning():
    # Qwen3.5 legt den Folge-Call gelegentlich komplett in den Reasoning-
    # Kanal -- der Normalizer promotet ihn zu einem echten tool_call.
    from langchain_core.messages import AIMessage
    rc = ('Die Teilagenten lieferten nichts. Ich suche direkt.\n'
          '<tool_call>\n<function=web_search>\n<parameter=query>\n'
          'Patch Tuesday Juli 2026 kritische CVEs\n</parameter>\n</function>\n</tool_call>')
    msg = AIMessage(content='', additional_kwargs={'reasoning_content': rc})
    assert msg.tool_calls and msg.tool_calls[0]['name'] == 'web_search'
    assert msg.tool_calls[0]['args']['query'] == 'Patch Tuesday Juli 2026 kritische CVEs'
    # Hat content dagegen echten Antworttext, bleiben Reasoning-Calls Gedanken.
    msg2 = AIMessage(content='Hier ist die fertige Antwort.',
                     additional_kwargs={'reasoning_content': rc})
    assert not msg2.tool_calls

def test_strip_status_lines():
    from rag_backend.main import strip_status_lines
    text = (
        "Ich recherchiere mal, wann Google mit einem eigenen Multimodal-Modell nachzieht.\n"
        "\n"
        "🔍 Suche im Web: \"Google multimodal model launch\"...\n"
        "📄 Lese Seite: https://blog.google/technology/ai/...\n"
        "Hier ist das echte Ergebnis.\n"
        "Es funktioniert prima!\n"
        "⏳ Mission 1: checking...\n"
        "🔧 calculate(expression=1+1)...\n"
        "Ende der Nachricht."
    )
    expected = (
        "Ich recherchiere mal, wann Google mit einem eigenen Multimodal-Modell nachzieht.\n"
        "\n"
        "Hier ist das echte Ergebnis.\n"
        "Es funktioniert prima!\n"
        "Ende der Nachricht."
    )
    assert strip_status_lines(text) == expected

def test_strip_status_lines_codepoint_variants():
    # Regression fuer byte-genaue Emoji-Mismatches: Labels ohne Variation
    # Selector und die Blackboard-Node-Zeilen muessen alle gestrippt werden.
    from rag_backend.main import strip_status_lines
    text = (
        "Echte Antwort davor.\n"
        "\U0001F550 Rufe aktuelle Uhrzeit ab...\n"
        "\U0001F324 Rufe Wetter ab: Bonn...\n"
        "\U0001F324\uFE0F Variante MIT Variation Selector...\n"
        "\U0001F5D1 Loesche Dokument: alt.pdf...\n"
        "\U0001F9F5 Starte 3 Recherche-Agenten (Wellen von 3)...\n"
        "\U0001F9E0 Solver-Agent analysiert und arbeitet an Loesung (Schritt 1)...\n"
        "\U0001F4DD Synthetisiere Endergebnis...\n"
        "Echte Antwort danach."
    )
    assert strip_status_lines(text) == "Echte Antwort davor.\nEchte Antwort danach."
