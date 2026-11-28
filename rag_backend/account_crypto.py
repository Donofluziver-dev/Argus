"""Verschluesselung je Konto (Umschlag-Verfahren).

Jedes Konto hat einen eigenen Datenschluessel (DEK, 256 Bit). Mit ihm sind seine Chats,
Werkzeugschritte, Missionen, Dateien, Ergebnisse und sein Gedaechtnis verschluesselt
(AES-256-GCM). Der Datenschluessel liegt nur verpackt in der Datenbank (Tabelle
user_keys); verpackt ist er mit dem Hauptschluessel (KEK), den nur rag-backend kennt:

- ARGUS_KEK_PROVIDER=local (Standard): KEK aus /run/secrets/kek (API_Tokens/kek.txt,
  von setup/init_stack.py angelegt, nur in rag-backend eingebunden).
- ARGUS_KEK_PROVIDER=azure-keyvault: KEK ist ein RSA-Schluessel in Azure Key Vault
  (AZURE_KEYVAULT_URL, AZURE_KEYVAULT_KEY, optional AZURE_KEYVAULT_KEY_VERSION). Er
  verlaesst den Key Vault nie; verpackt und entpackt wird dort (RSA-OAEP-256), angemeldet
  ueber die Managed Identity der VM (optional AZURE_CLIENT_ID fuer eine zugewiesene).

Was das schuetzt: eine Kopie der Datenbank, des Arbeitsordners oder einer Sicherung davon
ist ohne den Hauptschluessel unlesbar, und ein Datensatz laesst sich nur mit dem Schluessel
seines Kontos entschluesseln. Was es nicht schuetzt: wer rag-backend zur Laufzeit bedient
(docker exec, Speicherabzug), sieht die Daten wie das Backend selbst.

Formate:
- Datensatz: b"AK1\\0" + Nonce (12) + Chiffrat mit Tag; AAD "argus|rec|<konto>".
- Datei:     b"ARGUSF1\\n", dann Bloecke [Laenge u32][Nonce 12][Chiffrat] mit hoechstens
  1 MiB Klartext; AAD "argus|file|<konto>|<nr>|<letzter>". Umsortieren, Abschneiden
  und das Verschieben zu einem anderen Konto fallen beim Entschluesseln auf.
Aeltere Datensaetze (Fernet mit dem gemeinsamen Schluessel) und unverschluesselte Dateien
bleiben lesbar; migrate_all() stellt sie um.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Iterator, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("argus.account_crypto")

RECORD_PREFIX = b"AK1\x00"
FILE_MAGIC = b"ARGUSF1\n"
FILE_CHUNK = 1024 * 1024
_NONCE = 12
_TAG = 16
_CHUNK_OVERHEAD = 4 + _NONCE + _TAG
SERVICE_UID = 0


class AccountCryptoError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Hauptschluessel
# ---------------------------------------------------------------------------

class KeyProvider(Protocol):
    name: str

    def wrap(self, dek: bytes, user_id: int) -> tuple[str, bytes]: ...

    def unwrap(self, key_ref: str, wrapped: bytes, user_id: int) -> bytes: ...


def _b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class LocalKeyProvider:
    """KEK aus einer Datei, die nur rag-backend sieht. Verpackt mit AES-256-GCM, das
    Konto steht in der AAD: Ein verpackter Schluessel taugt nur fuer sein Konto."""

    name = "local"
    _PATHS = (Path("/run/secrets/kek"), Path("API_Tokens/kek.txt"))

    def __init__(self, kek: bytes | None = None):
        self._kek = kek if kek is not None else self._load()
        if len(self._kek) != 32:
            raise AccountCryptoError("Hauptschluessel (KEK) muss 32 Bytes haben.")
        self._ref = "local:v1:" + hashlib.sha256(self._kek).hexdigest()[:12]

    @classmethod
    def _load(cls) -> bytes:
        raw = (os.getenv("ARGUS_KEK") or "").strip()
        if not raw:
            for path in cls._PATHS:
                try:
                    raw = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if raw:
                    break
        if not raw:
            raise AccountCryptoError(
                "Hauptschluessel fehlt (API_Tokens/kek.txt). setup/init_stack.py legt ihn an.")
        try:
            return _b64url_decode(raw)
        except Exception:
            raise AccountCryptoError("Hauptschluessel (KEK) ist kein Base64-Wert.") from None

    def wrap(self, dek: bytes, user_id: int) -> tuple[str, bytes]:
        nonce = os.urandom(_NONCE)
        return self._ref, nonce + AESGCM(self._kek).encrypt(nonce, dek, f"argus-dek|{user_id}".encode())

    def unwrap(self, key_ref: str, wrapped: bytes, user_id: int) -> bytes:
        if key_ref != self._ref:
            raise AccountCryptoError(
                f"Datenschluessel von Konto {user_id} ist mit einem anderen Hauptschluessel verpackt.")
        return AESGCM(self._kek).decrypt(wrapped[:_NONCE], wrapped[_NONCE:], f"argus-dek|{user_id}".encode())


class AzureKeyVaultProvider:
    """KEK als RSA-Schluessel in Azure Key Vault. Anmeldung ueber die Managed Identity
    (Instance Metadata Service der VM). Kein Azure-SDK: zwei REST-Aufrufe genuegen."""

    name = "azure-keyvault"
    _API = "7.4"
    _IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"

    def __init__(self, vault_url: str | None = None, key_name: str | None = None,
                 key_version: str | None = None, client=None):
        self._vault = (vault_url or os.getenv("AZURE_KEYVAULT_URL") or "").strip().rstrip("/")
        self._key = (key_name or os.getenv("AZURE_KEYVAULT_KEY") or "").strip()
        self._version = (key_version or os.getenv("AZURE_KEYVAULT_KEY_VERSION") or "").strip()
        if not self._vault.startswith("https://") or not self._key:
            raise AccountCryptoError(
                "Azure Key Vault: AZURE_KEYVAULT_URL (https://...) und AZURE_KEYVAULT_KEY setzen.")
        self._client = client
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _http(self):
        if self._client is None:
            import httpx
            #--- trust_env=False: Weder IMDS noch der Key Vault gehen ueber einen Proxy
            #--- aus der Umgebung; der Token soll keinen Umweg nehmen.
            self._client = httpx.Client(timeout=15.0, trust_env=False)
        return self._client

    def _access_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] - 300 > time.time():
                return self._token[0]
            params = {"api-version": "2018-02-01", "resource": "https://vault.azure.net"}
            client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip()
            if client_id:
                params["client_id"] = client_id
            resp = self._http().get(self._IMDS, params=params, headers={"Metadata": "true"})
            if resp.status_code != 200:
                raise AccountCryptoError(f"Managed Identity: kein Token (HTTP {resp.status_code}).")
            data = resp.json()
            self._token = (data["access_token"], float(data.get("expires_on") or time.time() + 600))
            return self._token[0]

    def _post(self, url: str, body: dict) -> dict:
        resp = self._http().post(url, params={"api-version": self._API}, json=body,
                                 headers={"Authorization": f"Bearer {self._access_token()}"})
        if resp.status_code != 200:
            raise AccountCryptoError(f"Azure Key Vault: HTTP {resp.status_code} bei {url.rsplit('/', 1)[-1]}.")
        return resp.json()

    def wrap(self, dek: bytes, user_id: int) -> tuple[str, bytes]:
        base = f"{self._vault}/keys/{self._key}" + (f"/{self._version}" if self._version else "")
        data = self._post(f"{base}/wrapkey", {"alg": "RSA-OAEP-256", "value": _b64url(dek)})
        return "akv:" + data["kid"], _b64url_decode(data["value"])

    def unwrap(self, key_ref: str, wrapped: bytes, user_id: int) -> bytes:
        if not key_ref.startswith("akv:https://"):
            raise AccountCryptoError(f"Datenschluessel von Konto {user_id} stammt nicht aus Azure Key Vault.")
        kid = key_ref[4:]
        if not kid.startswith(self._vault + "/keys/"):
            raise AccountCryptoError(f"Datenschluessel von Konto {user_id} gehoert zu einem anderen Key Vault.")
        data = self._post(f"{kid}/unwrapkey", {"alg": "RSA-OAEP-256", "value": _b64url(wrapped)})
        return _b64url_decode(data["value"])


def make_provider() -> KeyProvider:
    name = (os.getenv("ARGUS_KEK_PROVIDER") or "local").strip().lower()
    if name == "local":
        return LocalKeyProvider()
    if name in ("azure-keyvault", "azure"):
        return AzureKeyVaultProvider()
    raise AccountCryptoError(f"ARGUS_KEK_PROVIDER '{name}' unbekannt (local oder azure-keyvault).")


# ---------------------------------------------------------------------------
# Datenschluessel je Konto
# ---------------------------------------------------------------------------

class DbKeyStore:
    """Verpackte Datenschluessel in der Tabelle user_keys."""

    def __init__(self, session_factory=None):
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from rag_backend.database import SessionLocal
        return SessionLocal()

    def get(self, uid: int) -> tuple[str, bytes] | None:
        from rag_backend.models import UserKey
        with self._session() as db:
            row = db.get(UserKey, uid)
            return (row.key_ref, bytes(row.wrapped_dek)) if row is not None else None

    def add(self, uid: int, provider: str, key_ref: str, wrapped: bytes) -> bool:
        """True, wenn eingetragen; False, wenn es schon einen gab (gleichzeitig angelegt)."""
        from sqlalchemy.exc import IntegrityError
        from rag_backend.models import UserKey
        with self._session() as db:
            db.add(UserKey(user_id=uid, provider=provider, key_ref=key_ref, wrapped_dek=wrapped))
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

    def delete(self, uid: int) -> None:
        from rag_backend.models import UserKey
        with self._session() as db:
            db.query(UserKey).filter_by(user_id=uid).delete()
            db.commit()


class MemoryKeyStore:
    """Verpackte Datenschluessel nur im Speicher (Tests)."""

    def __init__(self):
        self._rows: dict[int, tuple[str, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, uid: int) -> tuple[str, bytes] | None:
        with self._lock:
            return self._rows.get(uid)

    def add(self, uid: int, provider: str, key_ref: str, wrapped: bytes) -> bool:
        with self._lock:
            if uid in self._rows:
                return False
            self._rows[uid] = (key_ref, wrapped)
            return True

    def delete(self, uid: int) -> None:
        with self._lock:
            self._rows.pop(uid, None)


class AccountKeys:
    """Laedt, erzeugt und haelt die Datenschluessel. Entpackte Schluessel bleiben eine
    Weile im Speicher (DEK_CACHE_SECONDS), damit nicht jede Nachricht den Key Vault fragt."""

    def __init__(self, provider: KeyProvider | None = None, store=None,
                 cache_seconds: float | None = None):
        self._provider = provider
        self._store = store if store is not None else DbKeyStore()
        self._cache: dict[int, tuple[bytes, float]] = {}
        self._lock = threading.Lock()
        self._ttl = float(os.getenv("DEK_CACHE_SECONDS", "900")) if cache_seconds is None else cache_seconds

    @property
    def provider(self) -> KeyProvider:
        if self._provider is None:
            self._provider = make_provider()
        return self._provider

    def dek(self, user_id: int | None) -> bytes:
        uid = SERVICE_UID if user_id is None else int(user_id)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(uid)
            if hit and now - hit[1] < self._ttl:
                return hit[0]
        dek = self._load_or_create(uid)
        with self._lock:
            self._cache[uid] = (dek, now)
        return dek

    def _load_or_create(self, uid: int) -> bytes:
        row = self._store.get(uid)
        if row is not None:
            return self.provider.unwrap(row[0], row[1], uid)
        dek = AESGCM.generate_key(bit_length=256)
        key_ref, wrapped = self.provider.wrap(dek, uid)
        if self._store.add(uid, self.provider.name, key_ref, wrapped):
            log.info(f"Datenschluessel fuer Konto {uid} angelegt ({self.provider.name}).")
            return dek
        #--- Zwei Anfragen haben gleichzeitig angelegt: der zuerst gespeicherte gilt.
        row = self._store.get(uid)
        return self.provider.unwrap(row[0], row[1], uid)

    def forget(self, user_id: int) -> None:
        """Schluessel eines geloeschten Kontos aus Speicher und Datenbank entfernen."""
        with self._lock:
            self._cache.pop(int(user_id), None)
        self._store.delete(int(user_id))

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


_KEYS: AccountKeys | None = None
_KEYS_LOCK = threading.Lock()


def keys() -> AccountKeys:
    global _KEYS
    with _KEYS_LOCK:
        if _KEYS is None:
            _KEYS = AccountKeys()
        return _KEYS


def set_keys(manager: AccountKeys | None) -> None:
    """Fuer Tests und fuer einen Wechsel des Hauptschluessels zur Laufzeit."""
    global _KEYS
    with _KEYS_LOCK:
        _KEYS = manager


# ---------------------------------------------------------------------------
# Datensaetze
# ---------------------------------------------------------------------------

def _rec_aad(uid: int) -> bytes:
    return f"argus|rec|{uid}".encode()


def is_account_encrypted(data: bytes | None) -> bool:
    return bool(data) and bytes(data[:4]) == RECORD_PREFIX


def encrypt_for(user_id: int | None, text: str | None) -> bytes:
    if not text:
        return b""
    uid = SERVICE_UID if user_id is None else int(user_id)
    nonce = os.urandom(_NONCE)
    return RECORD_PREFIX + nonce + AESGCM(keys().dek(uid)).encrypt(nonce, text.encode("utf-8"), _rec_aad(uid))


def decrypt_for(user_id: int | None, data: bytes | None) -> str:
    if not data:
        return ""
    data = bytes(data)
    if not is_account_encrypted(data):
        from rag_backend.crypto_utils import decrypt_msg
        return decrypt_msg(data)
    uid = SERVICE_UID if user_id is None else int(user_id)
    body = data[len(RECORD_PREFIX):]
    try:
        plain = AESGCM(keys().dek(uid)).decrypt(body[:_NONCE], body[_NONCE:], _rec_aad(uid))
    except Exception:
        raise AccountCryptoError(f"Datensatz laesst sich mit dem Schluessel von Konto {uid} nicht entschluesseln.") from None
    return plain.decode("utf-8")


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------

def _file_aad(uid: int, index: int, last: bool) -> bytes:
    return f"argus|file|{uid}|{index}|{int(last)}".encode()


class EncryptedFileWriter:
    """Schreibt Klartext blockweise verschluesselt in ein offenes Binaer-Dateiobjekt."""

    def __init__(self, fp, user_id: int):
        self._fp = fp
        self._uid = int(user_id)
        self._aes = AESGCM(keys().dek(self._uid))
        self._buf = bytearray()
        self._index = 0
        self._closed = False
        fp.write(FILE_MAGIC)

    def _emit(self, chunk: bytes, last: bool) -> None:
        nonce = os.urandom(_NONCE)
        ct = self._aes.encrypt(nonce, bytes(chunk), _file_aad(self._uid, self._index, last))
        self._fp.write(struct.pack(">I", len(ct)) + nonce + ct)
        self._index += 1

    def write(self, data: bytes) -> int:
        self._buf += data
        #--- Mindestens ein Rest bleibt stehen: Erst close() weiss, welcher Block der letzte ist.
        while len(self._buf) > FILE_CHUNK:
            self._emit(self._buf[:FILE_CHUNK], last=False)
            del self._buf[:FILE_CHUNK]
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._emit(self._buf, last=True)
        self._buf = bytearray()
        self._closed = True


def is_encrypted_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fp:
            return fp.read(len(FILE_MAGIC)) == FILE_MAGIC
    except OSError:
        return False


def iter_file_plaintext(path: Path, user_id: int) -> Iterator[bytes]:
    """Klartext einer Datei in Bloecken; unverschluesselte (aeltere) Dateien unveraendert."""
    uid = int(user_id)
    with open(path, "rb") as fp:
        head = fp.read(len(FILE_MAGIC))
        if head != FILE_MAGIC:
            yield head
            while chunk := fp.read(FILE_CHUNK):
                yield chunk
            return
        aes = AESGCM(keys().dek(uid))
        index = 0
        header = fp.read(4)
        while header:
            if len(header) != 4:
                raise AccountCryptoError(f"{path.name}: verschluesselte Datei ist abgeschnitten.")
            (length,) = struct.unpack(">I", header)
            nonce = fp.read(_NONCE)
            ct = fp.read(length)
            if len(nonce) != _NONCE or len(ct) != length:
                raise AccountCryptoError(f"{path.name}: verschluesselte Datei ist abgeschnitten.")
            header = fp.read(4)
            last = not header
            try:
                yield aes.decrypt(nonce, ct, _file_aad(uid, index, last))
            except Exception:
                raise AccountCryptoError(
                    f"{path.name} laesst sich mit dem Schluessel von Konto {uid} nicht entschluesseln "
                    "(veraendert, abgeschnitten oder aus einem anderen Konto).") from None
            index += 1
        if index == 0:
            raise AccountCryptoError(f"{path.name}: verschluesselte Datei ohne Inhalt.")


def read_file_plaintext(path: Path, user_id: int) -> bytes:
    return b"".join(iter_file_plaintext(path, user_id))


def plaintext_size(path: Path) -> int:
    """Groesse des Klartexts, ohne zu entschluesseln (aus der Dateigroesse berechnet)."""
    size = path.stat().st_size
    if not is_encrypted_file(path):
        return size
    body = size - len(FILE_MAGIC)
    full = FILE_CHUNK + _CHUNK_OVERHEAD
    n_full, rest = divmod(body, full)
    if rest == 0 and n_full:
        n_full, rest = n_full - 1, full
    return n_full * FILE_CHUNK + max(0, rest - _CHUNK_OVERHEAD)


def write_encrypted_atomic(path: Path, data: bytes, user_id: int, mode: int = 0o600) -> None:
    """Ersetzt path atomar durch die verschluesselte Fassung von data."""
    tmp = path.with_name(f".{path.name}.enc-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as fp:
            writer = EncryptedFileWriter(fp, user_id)
            writer.write(data)
            writer.close()
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_text(path: Path, user_id: int) -> str:
    return read_file_plaintext(path, user_id).decode("utf-8")


def write_text(path: Path, text: str, user_id: int) -> None:
    write_encrypted_atomic(path, text.encode("utf-8"), user_id)


# ---------------------------------------------------------------------------
# Umstellung bestehender Daten
# ---------------------------------------------------------------------------

_MIGRATION_VERSION = 1


def _marker_path() -> Path:
    return Path(os.getenv("ACCOUNT_CRYPTO_MARKER", "/app/data/account_crypto.json"))


def _migrate_records(session_factory) -> dict[str, int]:
    from rag_backend.models import ChatSession, Message, Mission
    counts = {"nachrichten": 0, "missionen": 0}
    with session_factory() as db:
        owners = dict(db.query(ChatSession.id, ChatSession.user_id).all())
        last_id = 0
        while True:
            batch = (db.query(Message).filter(Message.id > last_id)
                     .order_by(Message.id.asc()).limit(500).all())
            if not batch:
                break
            for m in batch:
                last_id = m.id
                uid = owners.get(m.session_id)
                for col in ("content_encrypted", "steps_encrypted"):
                    raw = getattr(m, col)
                    if raw and not is_account_encrypted(raw):
                        try:
                            setattr(m, col, encrypt_for(uid, decrypt_for(uid, raw)))
                            counts["nachrichten"] += 1
                        except Exception as e:
                            log.warning(f"Nachricht {m.id}: Umstellung uebersprungen ({type(e).__name__}).")
            db.commit()
        for mission in db.query(Mission).all():
            for col in ("goal_encrypted", "context_encrypted", "pii_map_encrypted", "result_encrypted"):
                raw = getattr(mission, col)
                if raw and not is_account_encrypted(raw):
                    try:
                        setattr(mission, col, encrypt_for(mission.user_id, decrypt_for(mission.user_id, raw)))
                        counts["missionen"] += 1
                    except Exception as e:
                        log.warning(f"Mission {mission.id}: Umstellung uebersprungen ({type(e).__name__}).")
        db.commit()
    return counts


def account_files(user_dir: Path) -> Iterator[Path]:
    """Alle Dateien eines Kontos, die verschluesselt liegen: Dateien, Ergebnisse,
    USER.md, MEMORY.md samt Sicherung und das Tagesgedaechtnis."""
    for sub in ("Dateien", "Ergebnisse", "memory"):
        folder = user_dir / sub
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_file() and not p.is_symlink() and not p.name.startswith("."):
                    yield p
    for name in ("USER.md", "MEMORY.md", "MEMORY.md.bak"):
        p = user_dir / name
        if p.is_file() and not p.is_symlink():
            yield p


def _migrate_files(workspace: Path) -> int:
    users = workspace / "users"
    if not users.is_dir():
        return 0
    n = 0
    for user_dir in users.iterdir():
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        uid = int(user_dir.name)
        for p in account_files(user_dir):
            if is_encrypted_file(p):
                continue
            try:
                st = p.stat()
                write_encrypted_atomic(p, p.read_bytes(), uid)
                #--- mtime behalten: Danach richtet sich das Aufraeumen nach sieben Tagen.
                os.utime(p, (st.st_atime, st.st_mtime))
                n += 1
            except Exception as e:
                log.warning(f"Datei {p.name} (Konto {uid}): Umstellung uebersprungen ({type(e).__name__}).")
    return n


def migrate_all(workspace: Path | None = None, session_factory=None, force: bool = False) -> dict:
    """Stellt aeltere Daten auf die Schluessel je Konto um. Idempotent; ein Vermerk in
    /app/data verhindert, dass bei jedem Start alles neu gelesen wird."""
    marker = _marker_path()
    if not force:
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("version", 0) >= _MIGRATION_VERSION:
                return {"uebersprungen": True}
        except (OSError, ValueError):
            pass
    if session_factory is None:
        from rag_backend.database import SessionLocal as session_factory
    ws = workspace or Path(os.getenv("ARGUS_WORKSPACE", "/host/argus_workspace"))
    result = _migrate_records(session_factory)
    result["dateien"] = _migrate_files(ws)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"version": _MIGRATION_VERSION, "ts": time.time(), **result}),
                          encoding="utf-8")
    except OSError as e:
        log.warning(f"Vermerk der Umstellung nicht schreibbar: {e}")
    log.info(f"Verschluesselung je Konto: umgestellt {result}.")
    return result

