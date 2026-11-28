import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import base64
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rag_backend import account_crypto as ac
from rag_backend import user_files
from rag_backend.models import Base, ChatSession, Message, Mission, User, UserKey
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)

KEK = os.urandom(32)


@pytest.fixture
def keys():
    manager = ac.AccountKeys(provider=ac.LocalKeyProvider(KEK), store=ac.MemoryKeyStore())
    ac.set_keys(manager)
    yield manager
    ac.set_keys(None)


# ---- Datensaetze ------------------------------------------------------------

def test_record_roundtrip_and_binding_to_account(keys):
    blob = ac.encrypt_for(7, "Reklamation RK-2025-0041")
    assert blob.startswith(ac.RECORD_PREFIX) and b"RK-2025" not in blob
    assert ac.decrypt_for(7, blob) == "Reklamation RK-2025-0041"
    with pytest.raises(ac.AccountCryptoError):
        ac.decrypt_for(8, blob)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(ac.AccountCryptoError):
        ac.decrypt_for(7, tampered)


def test_two_accounts_have_different_keys(keys):
    assert keys.dek(1) != keys.dek(2)
    assert keys.dek(None) == keys.dek(ac.SERVICE_UID)


def test_legacy_fernet_records_stay_readable(keys):
    legacy = Fernet(os.environ["FERNET_KEY"].encode()).encrypt("alter Chat".encode())
    assert ac.decrypt_for(3, legacy) == "alter Chat"


def test_empty_values(keys):
    assert ac.encrypt_for(1, "") == b""
    assert ac.decrypt_for(1, b"") == ""


# ---- Dateien ----------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, ac.FILE_CHUNK - 1, ac.FILE_CHUNK, ac.FILE_CHUNK + 1,
                                  int(2.5 * ac.FILE_CHUNK), 3 * ac.FILE_CHUNK])
def test_file_roundtrip_and_plaintext_size(keys, tmp_path, size):
    data = os.urandom(size)
    path = user_files.write_new_file(tmp_path, "daten.bin", data, user_id=5)
    assert ac.is_encrypted_file(path)
    assert ac.plaintext_size(path) == size
    assert ac.read_file_plaintext(path, 5) == data


def test_file_tampering_truncation_and_wrong_account(keys, tmp_path):
    data = os.urandom(2 * ac.FILE_CHUNK + 10)
    path = user_files.write_new_file(tmp_path, "bericht.pdf", data, user_id=5)
    with pytest.raises(ac.AccountCryptoError):
        ac.read_file_plaintext(path, 6)
    raw = path.read_bytes()
    #--- Den letzten Block abschneiden: Der vorletzte war nicht als letzter verschluesselt.
    last = len(raw) - (10 + 32)
    path.write_bytes(raw[:last])
    with pytest.raises(ac.AccountCryptoError):
        ac.read_file_plaintext(path, 5)
    flipped = bytearray(raw)
    flipped[len(ac.FILE_MAGIC) + 40] ^= 1
    path.write_bytes(bytes(flipped))
    with pytest.raises(ac.AccountCryptoError):
        ac.read_file_plaintext(path, 5)


def test_legacy_plaintext_file_is_read_as_is(keys, tmp_path):
    path = tmp_path / "alt.csv"
    path.write_bytes(b"a,b\n1,2\n")
    assert ac.read_file_plaintext(path, 5) == b"a,b\n1,2\n"
    assert ac.plaintext_size(path) == 8
    with user_files.plaintext_copy(5, path) as copy:
        assert copy == path


def test_plaintext_copy_disappears(keys, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PLAINTEXT_DIR", str(tmp_path / "klartext"))
    (tmp_path / "klartext").mkdir()
    path = user_files.write_new_file(tmp_path, "tabelle.csv", b"x,y\n", user_id=9)
    with user_files.plaintext_copy(9, path) as copy:
        assert copy.read_bytes() == b"x,y\n" and copy.name == "tabelle.csv"
        assert oct(copy.stat().st_mode & 0o777) == oct(0o600)
    assert list((tmp_path / "klartext").iterdir()) == []


# ---- Hauptschluessel lokal ----------------------------------------------------

def test_local_provider_rejects_other_kek_and_other_account():
    a, b = ac.LocalKeyProvider(os.urandom(32)), ac.LocalKeyProvider(os.urandom(32))
    ref, wrapped = a.wrap(b"k" * 32, 4)
    assert a.unwrap(ref, wrapped, 4) == b"k" * 32
    with pytest.raises(ac.AccountCryptoError):
        b.unwrap(ref, wrapped, 4)
    with pytest.raises(Exception):
        a.unwrap(ref, wrapped, 5)


def test_local_provider_reads_kek_from_env(monkeypatch):
    monkeypatch.setenv("ARGUS_KEK", base64.urlsafe_b64encode(KEK).decode().rstrip("="))
    assert ac.LocalKeyProvider()._kek == KEK
    monkeypatch.setenv("ARGUS_KEK", "zu-kurz")
    with pytest.raises(ac.AccountCryptoError):
        ac.LocalKeyProvider()


# ---- Hauptschluessel in Azure Key Vault ----------------------------------------

class _FakeAzure:
    """Instance Metadata Service und Key Vault mit einem echten RSA-Schluessel."""

    VAULT = "https://argus-test.vault.azure.net"

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"{self.VAULT}/keys/argus-kek/0123456789abcdef"
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        if request.url.host == "169.254.169.254":
            assert request.headers.get("metadata") == "true"
            assert request.url.params["resource"] == "https://vault.azure.net"
            return httpx.Response(200, json={"access_token": "token-123", "expires_on": str(int(time.time()) + 3600)})
        assert request.headers.get("authorization") == "Bearer token-123"
        assert request.url.params["api-version"] == "7.4"
        body = json.loads(request.content)
        assert body["alg"] == "RSA-OAEP-256"
        value = ac._b64url_decode(body["value"])
        if request.url.path.endswith("/wrapkey"):
            return httpx.Response(200, json={"kid": self.kid,
                                             "value": ac._b64url(self.key.public_key().encrypt(value, oaep))})
        if request.url.path == "/keys/argus-kek/0123456789abcdef/unwrapkey":
            return httpx.Response(200, json={"kid": self.kid, "value": ac._b64url(self.key.decrypt(value, oaep))})
        return httpx.Response(404)


def test_azure_keyvault_wrap_and_unwrap():
    fake = _FakeAzure()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    provider = ac.AzureKeyVaultProvider(fake.VAULT, "argus-kek", client=client)
    manager = ac.AccountKeys(provider=provider, store=ac.MemoryKeyStore())
    ac.set_keys(manager)
    try:
        blob = ac.encrypt_for(11, "Lastenheft Kunde A")
        manager.clear_cache()
        assert ac.decrypt_for(11, blob) == "Lastenheft Kunde A"
    finally:
        ac.set_keys(None)
    paths = [c.url.path for c in fake.calls]
    assert paths.count("/metadata/identity/oauth2/token") == 1
    assert "/keys/argus-kek/wrapkey" in paths
    assert "/keys/argus-kek/0123456789abcdef/unwrapkey" in paths


def test_azure_keyvault_refuses_foreign_vault_and_missing_config():
    fake = _FakeAzure()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    provider = ac.AzureKeyVaultProvider(fake.VAULT, "argus-kek", client=client)
    with pytest.raises(ac.AccountCryptoError):
        provider.unwrap("akv:https://fremd.vault.azure.net/keys/x/1", b"x", 1)
    with pytest.raises(ac.AccountCryptoError):
        ac.AzureKeyVaultProvider("", "argus-kek")


def test_provider_selection(monkeypatch):
    monkeypatch.setenv("ARGUS_KEK_PROVIDER", "unbekannt")
    with pytest.raises(ac.AccountCryptoError):
        ac.make_provider()


# ---- Schluessel in der Datenbank ---------------------------------------------

@pytest.fixture
def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'keys.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_db_store_creates_once_even_when_racing(sqlite_session):
    manager = ac.AccountKeys(provider=ac.LocalKeyProvider(KEK), store=ac.DbKeyStore(sqlite_session))
    results = []

    def take():
        results.append(manager._load_or_create(21))

    threads = [threading.Thread(target=take) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    with sqlite_session() as db:
        assert db.query(UserKey).filter_by(user_id=21).count() == 1
        row = db.get(UserKey, 21)
        assert row.provider == "local" and row.key_ref.startswith("local:v1:")
    manager.forget(21)
    with sqlite_session() as db:
        assert db.get(UserKey, 21) is None


# ---- Umstellung bestehender Daten ---------------------------------------------

def test_migration_converts_records_and_files(sqlite_session, tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_CRYPTO_MARKER", str(tmp_path / "marker.json"))
    manager = ac.AccountKeys(provider=ac.LocalKeyProvider(KEK), store=ac.DbKeyStore(sqlite_session))
    ac.set_keys(manager)
    fernet = Fernet(os.environ["FERNET_KEY"].encode())
    try:
        with sqlite_session() as db:
            db.add(User(id=3, email="anna", hashed_password="x", role="user"))
            db.add(ChatSession(id=30, user_id=3))
            db.add(Message(id=300, session_id=30, sender="user", content_encrypted=fernet.encrypt(b"alte Frage"),
                           steps_encrypted=fernet.encrypt(b'{"schritte": []}')))
            db.add(Mission(id=1, user_id=3, goal_encrypted=fernet.encrypt(b"Ziel"), provider="gemini"))
            db.commit()
        user_dir = tmp_path / "ws" / "users" / "3"
        (user_dir / "Dateien").mkdir(parents=True)
        (user_dir / "Dateien" / "liste.csv").write_bytes(b"teil;menge\n")
        (user_dir / "MEMORY.md").write_text("- arbeitet in der Qualitaet\n", encoding="utf-8")
        os.utime(user_dir / "Dateien" / "liste.csv", (1_700_000_000, 1_700_000_000))

        result = ac.migrate_all(workspace=tmp_path / "ws", session_factory=sqlite_session)
        assert result["nachrichten"] == 2 and result["missionen"] == 1 and result["dateien"] == 2

        with sqlite_session() as db:
            m = db.get(Message, 300)
            assert ac.is_account_encrypted(m.content_encrypted)
            assert ac.decrypt_for(3, m.content_encrypted) == "alte Frage"
            assert ac.decrypt_for(3, db.get(Mission, 1).goal_encrypted) == "Ziel"
        csv = user_dir / "Dateien" / "liste.csv"
        assert ac.is_encrypted_file(csv) and ac.read_file_plaintext(csv, 3) == b"teil;menge\n"
        assert int(csv.stat().st_mtime) == 1_700_000_000
        assert ac.read_text(user_dir / "MEMORY.md", 3).startswith("- arbeitet")
        assert ac.migrate_all(workspace=tmp_path / "ws", session_factory=sqlite_session) == {"uebersprungen": True}
    finally:
        ac.set_keys(None)


# ---- Ueber die Oberflaeche -----------------------------------------------------

def test_upload_lands_encrypted_and_downloads_as_plaintext(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)
    _make_account(client, "anna", "einLangesPasswort", "admin")
    uid = client.get("/v1/dashboard/auth/state").json()["user_id"]
    inhalt = "Pruefprotokoll CP25, Merkmal 4 ausserhalb".encode() * 1000
    r = client.post("/v1/dashboard/files", headers=DASH_ORIGIN,
                    files={"file": ("protokoll.txt", inhalt, "text/plain")})
    assert r.status_code == 200, r.text
    on_disk = tmp_path / "users" / str(uid) / "Dateien" / "protokoll.txt"
    assert ac.is_encrypted_file(on_disk) and b"Pruefprotokoll" not in on_disk.read_bytes()
    listing = client.get("/v1/dashboard/files").json()
    assert listing["dateien"][0]["size"] == len(inhalt)
    dl = client.get("/v1/dashboard/files/dateien/protokoll.txt")
    assert dl.status_code == 200 and dl.content == inhalt
    assert dl.headers["content-length"] == str(len(inhalt))


@pytest.mark.asyncio
async def test_read_document_reads_encrypted_account_file(tmp_path, monkeypatch, keys):
    from rag_backend import agent
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(user_files, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setenv("ARGUS_PLAINTEXT_DIR", str(tmp_path / "klartext"))
    (tmp_path / "klartext").mkdir()
    folder = user_files.get_user_dir(12, "dateien", create=True)
    user_files.write_new_file(folder, "notiz.txt", "Lieferant Saxonia, Bewertung B".encode(), user_id=12)
    agent.request_origin_var.set({"user_id": 12, "user_role": "user"})
    out = await agent.read_document.ainvoke({"path": "notiz.txt"})
    assert "Lieferant Saxonia" in out
    assert list((tmp_path / "klartext").iterdir()) == []


def test_memory_is_stored_encrypted(tmp_path, monkeypatch, keys):
    from rag_backend.memory import EpisodicMemory
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    mem = EpisodicMemory()
    monkeypatch.setattr(mem, "_user_exists", lambda uid: True)
    mem._write_memory(4, "- Name: Anna\n")
    mem._write_memory(4, "- Name: Anna, Qualitaet\n")
    path = Path(tmp_path) / "users" / "4" / "MEMORY.md"
    assert b"Anna" not in path.read_bytes() and b"Anna" not in path.with_suffix(".md.bak").read_bytes()
    assert mem._read_memory(4) == "- Name: Anna, Qualitaet\n"
    assert ac.read_text(path.with_suffix(".md.bak"), 4) == "- Name: Anna\n"


def test_account_deletion_forgets_key(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                    json={"username": "gast", "password": "einLangesPasswort", "role": "user"})
    gast = r.json()["id"]
    ac.keys().dek(gast)
    assert ac.keys()._store.get(gast) is not None
    assert client.delete(f"/v1/dashboard/users/{gast}", headers=DASH_ORIGIN).status_code == 200
    assert ac.keys()._store.get(gast) is None
