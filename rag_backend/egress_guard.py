"""Egress-Guard fuer Gespräche mit vertraulichen Daten (Aufgabe 1).

Bedrohungsmodell:
Sobald ein Gespräch vertrauliche Daten gelesen hat (Qdrant, lokale Dokumente,
Dateisystem-MCP oder lesende Action-Engine-Werkzeuge), wird das Gespräch als
vertraulich markiert. In einem markierten Gespräch werden alle Wege nach außen
(Websuche, Webseiten-Abruf, Wetter, Routenberechnung, Cloud-Aufrufe/Missionen,
externe MCP-Werkzeuge) abhängig von EGRESS_AFTER_SENSITIVE ("block" | "allow" | "confirm")
kontrolliert und einer Inhaltsprüfung (Fingerabdrücke aus Kennungen, E-Mails,
IBANs und 4-Wort-Folgen) unterzogen.
"""
from __future__ import annotations

import base64
import codecs
import contextlib
import gzip
import hashlib
import html
import json
import logging
import math
import os
import re
import unicodedata
import zlib
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote, unquote_plus

log = logging.getLogger(__name__)

BLOCK_MESSAGE = (
    "Websuche und andere Zugriffe nach aussen sind in diesem Gespraech gesperrt, "
    "weil es Firmendokumente gelesen hat. Fuer eine Recherche ein neues Gespraech beginnen."
)

CONTENT_BLOCK_MESSAGE = (
    "Websuche und andere Zugriffe nach aussen sind in diesem Gespraech gesperrt, "
    "weil die Anfrage vertrauliche Bestandteile aus gelesenen Firmendokumenten enthaelt. "
    "Fuer eine Recherche ein neues Gespraech beginnen."
)

CONFIRM_DENIED_MESSAGE = (
    "Websuche und andere Zugriffe nach aussen sind in diesem Gespraech gesperrt "
    "(Freigabe nicht erteilt), weil es Firmendokumente gelesen hat. "
    "Fuer eine Recherche ein neues Gespraech beginnen."
)

SENSITIVE_SOURCE_TOOLS = frozenset({
    "document_search",
    "read_document",
    "list_indexed_documents",
    "index_document",
    "ingest_document",
    "fs_read_file",
    "fs_list_directory",
    "execute_code",  # Sandbox liest C:\Argus_Workspace
})

LOCAL_FS_MCP_TOOLS = frozenset({
    "fs_read_file",
    "fs_write_file",
    "fs_list_directory",
    "fs_mkdir",
})

LOCAL_FS_MCP_READ_TOOLS = frozenset({
    "fs_read_file",
    "fs_list_directory",
})

EGRESS_TOOLS = frozenset({
    "web_search",
    "read_webpage",
    "deep_research",
    "research",
    "_search_and_read",
    "get_weather",
    "route_distance",
    "cloud_ask",
    "start_mission",
})


class EgressState:
    """Veränderlicher Gesprächszustand für den Egress-Guard.

    Wird über ContextVar an Unteragenten (invoke_subagent, parallel_agents,
    research-Wellen) als dieselbe Objektreferenz vererbt: liest das Eltern-
    gespräch vertrauliche Daten, sieht der Unteragent die Markierung; liest
    ein Unteragent vertrauliche Daten, mutiert er dasselbe EgressState-Objekt
    und markiert damit auch das Elterngespräch.
    """

    def __init__(self, session_key: str = "default", marked: bool = False, db_checked: bool = False):
        self.session_key: str = session_key
        self.marked: bool = marked
        self.db_checked: bool = db_checked
        # Nur SHA-256-Hex-Digests, niemals Klartext!
        self.fingerprints: set[str] = set()
        self.fingerprint_kinds: dict[str, str] = {}
        # True, wenn die Fingerabdruecke nicht alles abdecken, was gelesen
        # wurde: Obergrenze erreicht, oder die Markierung kam nach einem
        # Neustart aus der Datenbank. "allow" wirkt dann wie "block".
        self.fingerprints_incomplete: bool = False
        self.sources: list[str] = []


# ContextVar hält die Referenz auf das aktuelle EgressState-Objekt.
egress_state_var: ContextVar[EgressState | None] = ContextVar("argus_egress_state", default=None)

# Verhindert doppelte Prüfung/Auditierung, wenn z.B. web_search intern _search_and_read aufruft.
_in_checked_egress_tool_var: ContextVar[bool] = ContextVar("argus_in_checked_egress_tool", default=False)

# In-Memory-Speicher je Gesprächsschlüssel (überlebt mehrere Turns bis zum Neustart).
# Begrenzt, sonst waechst er mit jedem Gespraech: die aeltesten fallen heraus.
# Ein herausgefallenes Gespraech liest seine Markierung beim naechsten Zugriff
# wieder aus der Datenbank; nur seine Fingerabdruecke sind dann weg (relevant
# allein fuer "allow").
_SESSION_STATES: OrderedDict[str, EgressState] = OrderedDict()
_MAX_SESSION_STATES = 256
_MAX_FINGERPRINTS = 100_000


def _state_for(key: str, *, db_checked: bool = False) -> EgressState:
    state = _SESSION_STATES.get(key)
    if state is None:
        state = EgressState(session_key=key, db_checked=db_checked)
        _SESSION_STATES[key] = state
        while len(_SESSION_STATES) > _MAX_SESSION_STATES:
            _SESSION_STATES.popitem(last=False)
    else:
        _SESSION_STATES.move_to_end(key)
    return state

# Optionaler Session-Factory-Override für Tests.
_CUSTOM_SESSION_FACTORY = None


@contextlib.contextmanager
def EgressCheckedScope():
    """Markiert den aktuellen Aufrufpfad als bereits durch das äußere Egress-Tool geprüft."""
    token = _in_checked_egress_tool_var.set(True)
    try:
        yield
    finally:
        _in_checked_egress_tool_var.reset(token)


def is_in_checked_egress_scope() -> bool:
    return bool(_in_checked_egress_tool_var.get())


@contextlib.contextmanager
def _open_db_session():
    """Öffnet eine DB-Session unter Berücksichtigung von Test-Overrides."""
    if _CUSTOM_SESSION_FACTORY is not None:
        db = _CUSTOM_SESSION_FACTORY()
        try:
            yield db
        finally:
            db.close()
        return

    try:
        from rag_backend.main import app, get_db
        override = app.dependency_overrides.get(get_db)
        if override is not None:
            gen_or_sess = override()
            if hasattr(gen_or_sess, "__next__"):
                db = next(gen_or_sess)
                try:
                    yield db
                finally:
                    try:
                        next(gen_or_sess)
                    except StopIteration:
                        pass
                return
            elif gen_or_sess is not None:
                yield gen_or_sess
                return
    except Exception:
        pass

    from rag_backend.database import SessionLocal
    with SessionLocal() as db:
        yield db


def _load_marked_from_db(session_key: str) -> bool:
    """Liest die persistierte Gesprächsmarkierung aus PostgreSQL/SQLite."""
    if not session_key:
        return False
    try:
        if session_key.startswith("dashboard:"):
            sid_str = session_key.split(":", 1)[1]
            sid = int(sid_str)
            from rag_backend.models import ChatSession
            with _open_db_session() as db:
                row = db.get(ChatSession, sid)
                if row is not None and bool(getattr(row, "has_sensitive_data", False)):
                    return True
    except Exception as e:
        log.debug(f"Egress-Guard DB-Load ({session_key}) nicht verfügbar: {e}")
    return False


def _save_marked_to_db(session_key: str, marked: bool = True) -> None:
    """Schreibt die Gesprächsmarkierung über Neustarts hinweg in die Datenbank."""
    if not session_key:
        return
    try:
        if session_key.startswith("dashboard:"):
            sid_str = session_key.split(":", 1)[1]
            sid = int(sid_str)
            from rag_backend.models import ChatSession
            with _open_db_session() as db:
                row = db.get(ChatSession, sid)
                if row is not None:
                    row.has_sensitive_data = bool(marked)
                    db.commit()
    except Exception as e:
        log.debug(f"Egress-Guard DB-Save ({session_key}) fehlgeschlagen: {e}")


def _resolve_origin_session_key(origin: dict | None = None) -> str | None:
    """Bestimmt den eindeutigen Gesprächsschlüssel aus request_origin_var."""
    if origin is None:
        try:
            from rag_backend.agent import request_origin_var
            origin = request_origin_var.get()
        except Exception:
            origin = None
    if not isinstance(origin, dict):
        return None
    if origin.get("dashboard_session_id") is not None:
        return f"dashboard:{origin['dashboard_session_id']}"
    if origin.get("session_id") is not None:
        return f"dashboard:{origin['session_id']}"
    if origin.get("session_key"):
        return str(origin["session_key"])
    return None


def _default_session_key() -> str:
    test_id = os.environ.get("PYTEST_CURRENT_TEST")
    if test_id:
        # Nur der Test-Name ohne Phase (" (setup)" / " (call)" / " (teardown)")
        base_test = test_id.split(" (")[0]
        return f"test:{base_test}"
    return "default:global"


def get_current_egress_state() -> EgressState:
    """Liefert den aktiven EgressState des aktuellen Gesprächs (inkl. DB-Sync)."""
    explicit_key = _resolve_origin_session_key()
    ctx_state = egress_state_var.get()

    if explicit_key is not None:
        if ctx_state is not None and ctx_state.session_key == explicit_key:
            state = ctx_state
        else:
            state = _state_for(explicit_key)
            egress_state_var.set(state)

        if not state.marked and explicit_key.startswith("dashboard:"):
            if _load_marked_from_db(explicit_key):
                state.marked = True
                state.fingerprints_incomplete = True
            state.db_checked = True
        return state

    if ctx_state is not None:
        if not ctx_state.marked and ctx_state.session_key.startswith("dashboard:"):
            if _load_marked_from_db(ctx_state.session_key):
                ctx_state.marked = True
                ctx_state.fingerprints_incomplete = True
            ctx_state.db_checked = True
        return ctx_state

    fallback_key = _default_session_key()
    state = _state_for(fallback_key, db_checked=True)
    egress_state_var.set(state)
    return state


def activate_conversation_state(
    origin: dict | None = None,
    *,
    dashboard_session_id: int | None = None,
    fresh_if_anonymous: bool = False,
) -> EgressState:
    """Initialisiert und bindet den EgressState zu Beginn eines Gesprächs-Turns."""
    if dashboard_session_id is not None:
        key = f"dashboard:{dashboard_session_id}"
    else:
        key = _resolve_origin_session_key(origin)

    if key is not None:
        state = _state_for(key)
        if not state.marked and key.startswith("dashboard:"):
            if _load_marked_from_db(key):
                state.marked = True
                state.fingerprints_incomplete = True
            state.db_checked = True
        egress_state_var.set(state)
        return state

    fallback_key = _default_session_key()
    if fresh_if_anonymous:
        _SESSION_STATES.pop(fallback_key, None)
    state = _state_for(fallback_key, db_checked=True)
    egress_state_var.set(state)
    return state


def clear_memory_state() -> None:
    """Leert den In-Memory-Zustand (z.B. zur Simulation eines Backend-Neustarts in Tests).
    Die Datenbank-Spalten bleiben unberührt."""
    _SESSION_STATES.clear()
    egress_state_var.set(None)


def reset_egress_state(session_key: str | None = None) -> None:
    """Setzt die Markierung und Fingerabdrücke im Speicher (und optional in der DB) zurück."""
    if session_key is not None:
        _SESSION_STATES.pop(session_key, None)
        _save_marked_to_db(session_key, False)
        cur = egress_state_var.get()
        if cur is not None and cur.session_key == session_key:
            egress_state_var.set(None)
        return

    _SESSION_STATES.clear()
    egress_state_var.set(None)


def is_block_message(text: Any) -> bool:
    """True, wenn ein Werkzeug-Ergebnis die Abweisung durch die Sperre ist. Nur am
    Anfang geprueft: ein langer Bericht, der eine Abweisung zitiert, zaehlt nicht."""
    if not isinstance(text, str):
        return False
    t = text.lstrip()
    return t.startswith((BLOCK_MESSAGE, CONTENT_BLOCK_MESSAGE, CONFIRM_DENIED_MESSAGE))


def is_conversation_sensitive() -> bool:
    """True, sobald das aktuelle Gespräch vertrauliche Daten gelesen hat."""
    return get_current_egress_state().marked


# ---------------------------------------------------------------------------
# Fingerabdrücke & Inhaltsprüfung (Aufgabe 1d)
# ---------------------------------------------------------------------------

_WRAPPER_CLEAN_RE = re.compile(
    r"</?untrusted_(?:document_content|web_content|cloud_response)>|"
    r"</?web_source[^>]*>|"
    r"^Retrieved internal documents:\s*$|"
    r"^Currently indexed documents in Qdrant[^:]*:\s*$|"
    r"^---\s*Content of .*?---\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")

# IBAN: 2 Buchstaben Ländercode + 2 Prüfziffern + 11..30 alphanumerische Zeichen (mit/ohne Leerzeichen/Bindestrich)
_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[\s-]?[A-Z0-9]{4}){2,7}(?:[\s-]?[A-Z0-9]{1,4})?\b",
    re.IGNORECASE,
)

# Kennungen/Zahlen >= 5 Zeichen mit mindestens einer Ziffer
_COMPOUND_ID_RE = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9._/-]{3,}[a-zA-Z0-9]\b")
_SIMPLE_ID_RE = re.compile(r"[a-zA-Z0-9_-]{5,}")
_DIGIT_SEQ_RE = re.compile(r"\d{5,}")

# Wörter für 4-Wort-Folgen
_WORD_RE = re.compile(r"[a-zA-Z0-9äöüÄÖÜß]+")

_IGNORED_IDS = frozenset({
    "127.0.0.1",
    "127001",
    "0.0.0.0",
    "00000",
    "utf-8",
    "utf8",
})


def _hash_item(kind: str, normalized_value: str) -> str:
    return hashlib.sha256(f"{kind}:{normalized_value}".encode("utf-8")).hexdigest()


def _decode_variants(raw_text: str) -> list[str]:
    """Löst HTML-Entities und URL-Kodierung (%xx sowie +) iterativ auf."""
    if not raw_text:
        return []
    cleaned = _WRAPPER_CLEAN_RE.sub(" ", raw_text)
    cleaned = html.unescape(cleaned)

    v_pct = cleaned
    v_plus = cleaned
    for _ in range(3):
        next_pct = unquote(v_pct)
        if next_pct == v_pct:
            break
        v_pct = next_pct

    for _ in range(3):
        next_plus = unquote_plus(v_plus)
        if next_plus == v_plus:
            break
        v_plus = next_plus

    out = []
    for v in (cleaned, v_pct, v_plus):
        norm = re.sub(r"\s+", " ", v).strip()
        if norm and norm not in out:
            out.append(norm)
    return out


def _flatten_strings(obj: Any) -> list[str]:
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj] if obj else []
    if isinstance(obj, (bytes, bytearray)):
        try:
            return [obj.decode("utf-8", errors="ignore")]
        except Exception:
            return []
    if isinstance(obj, dict):
        res = []
        for v in obj.values():
            res.extend(_flatten_strings(v))
        return res
    if isinstance(obj, (list, tuple, set, frozenset)):
        res = []
        for item in obj:
            res.extend(_flatten_strings(item))
        return res
    if hasattr(obj, "model_dump"):
        return _flatten_strings(obj.model_dump())
    return [str(obj)]


def extract_sensitive_hashes(content: Any) -> dict[str, str]:
    """Extrahiert gehashte Fingerabdrücke (SHA-256 -> Kategorie) aus Text/Daten."""
    hashes: dict[str, str] = {}
    for raw in _flatten_strings(content):
        for variant in _decode_variants(raw):
            low = variant.lower()

            # 1. E-Mail-Adressen
            for m in _EMAIL_RE.finditer(low):
                email_val = m.group(0).strip().lower()
                if len(email_val) >= 5:
                    h = _hash_item("email", email_val)
                    hashes[h] = "email"

            # 2. IBANs (mit und ohne Leerzeichen/Trennzeichen)
            for m in _IBAN_RE.finditer(variant):
                compact_iban = re.sub(r"[\s-]+", "", m.group(0)).lower()
                if 15 <= len(compact_iban) <= 34 and compact_iban[:2].isalpha() and compact_iban[2:4].isdigit():
                    h = _hash_item("iban", compact_iban)
                    hashes[h] = "iban"

            # 3. Zahlen und Kennungen >= 5 Zeichen mit mindestens einer Ziffer
            for regex in (_COMPOUND_ID_RE, _SIMPLE_ID_RE, _DIGIT_SEQ_RE):
                for m in regex.finditer(low):
                    tok = m.group(0).strip("._/-")
                    if len(tok) >= 5 and any(c.isdigit() for c in tok) and tok not in _IGNORED_IDS:
                        hashes[_hash_item("id", tok)] = "id"
                        compact = re.sub(r"[._/-]+", "", tok)
                        if len(compact) >= 5 and any(c.isdigit() for c in compact) and compact not in _IGNORED_IDS:
                            hashes[_hash_item("id", compact)] = "id"

            # 4. Folgen von 4 oder mehr Wörtern am Stück (als 4-Wort-Fenster)
            words_alpha = _WORD_RE.findall(low)
            if len(words_alpha) >= 4:
                for i in range(len(words_alpha) - 3):
                    phrase = " ".join(words_alpha[i : i + 4])
                    hashes[_hash_item("phrase", phrase)] = "phrase"

            words_ws = [
                w.strip(".,;:!?()\"'[]{}<>")
                for w in low.split()
            ]
            words_ws = [w for w in words_ws if w]
            if len(words_ws) >= 4:
                for i in range(len(words_ws) - 3):
                    phrase = " ".join(words_ws[i : i + 4])
                    hashes[_hash_item("phrase", phrase)] = "phrase"

    return hashes


def mark_sensitive_source(source_name: str, content: Any = None) -> EgressState:
    """Markiert das aktuelle Gespräch als vertraulich und speichert Fingerabdrücke."""
    state = get_current_egress_state()
    was_marked = state.marked
    state.marked = True
    if source_name and source_name not in state.sources:
        state.sources.append(source_name)

    if content is not None:
        extracted = extract_sensitive_hashes(content)
        for h, kind in extracted.items():
            if h in state.fingerprints:
                continue
            if len(state.fingerprints) >= _MAX_FINGERPRINTS:
                state.fingerprints_incomplete = True
                break
            state.fingerprints.add(h)
            state.fingerprint_kinds[h] = kind

    if not was_marked or state.session_key.startswith("dashboard:"):
        _save_marked_to_db(state.session_key, True)

    return state


_FORMATTED_HEX_RES = (
    re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}"),
    re.compile(r"(?:0x[0-9a-fA-F]{2}[ ,:]*){8,}"),
    re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){7,}\b"),
    re.compile(r"\b[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2}){7,}\b"),
)
_HEX_CLEAN_RE = re.compile(r"0x|\\x|[: ,]")
_SPACED_ID_RE = re.compile(
    r"(?<![a-zA-Z0-9äöüÄÖÜß])([a-zA-Z0-9äöüÄÖÜß](?:([^a-zA-Z0-9äöüÄÖÜß])[a-zA-Z0-9äöüÄÖÜß])(?:\2[a-zA-Z0-9äöüÄÖÜß]){3,})(?![a-zA-Z0-9äöüÄÖÜß])"
)
_CONTIGUOUS_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,6}")
_URL_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")


def _entropy_bits_per_char(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_random(token: str, pure_hex: bool) -> bool:
    '''Sieht eine lange, nicht lesbar dekodierbare Folge nach verschluesselten oder
    gepackten Daten aus? Woerter, Pfade und URL-Titel ("mittelstand-investiert-in-...")
    bestehen auch aus Zeichen des Base64-Alphabets, haben aber deutlich weniger Entropie
    (Text um 4 Bit je Zeichen, zufaelliges Base64 ueber 5) und selten alle drei
    Zeichenklassen. Zufaellige Kennungen ab 64 Zeichen (etwa Tracking-Parameter) sperrt
    die Regel in markierten Gespraechen mit: Von verschluesselten Daten sind sie nicht zu
    unterscheiden.'''
    body = token.rstrip("=")
    if pure_hex:
        return len(body) >= 72 and _entropy_bits_per_char(body.lower()) >= 3.5
    if len(body) < 64 or _URL_SLUG_RE.fullmatch(token):
        return False
    if re.fullmatch(r"[A-Z2-7]+", body):                       # Base32
        return _entropy_bits_per_char(body) >= 4.2
    mixed = (any(c.islower() for c in body) and any(c.isupper() for c in body)
             and any(c.isdigit() for c in body))
    return mixed and _entropy_bits_per_char(body) >= 4.8


def _is_predominantly_printable(text: str, min_ratio: float = 0.85) -> bool:
    if not text:
        return False
    if "\x00" in text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return (printable / len(text)) >= min_ratio


class _Oversized(Exception):
    '''Gepackte Daten ergeben mehr als die Obergrenze: der Rest bliebe ungeprueft.'''


def _decompress_up_to_1mb(data: bytes, max_out: int = 1_048_576) -> bytes | None:
    '''Entpackt gzip, zlib oder raw deflate, hoechstens max_out Bytes (Zip-Bomben).
    Ist dann noch etwas uebrig, wirft es _Oversized: Hinter 1 MB Fuellmaterial koennten
    Firmendaten stehen, die sonst nie geprueft wuerden.'''
    attempts = []
    if data.startswith(b"\x1f\x8b"):
        attempts.append(16 + zlib.MAX_WBITS)
    if len(data) >= 2 and (data[0] & 0x0F) == 8 and (data[0] * 256 + data[1]) % 31 == 0:
        attempts.append(zlib.MAX_WBITS)
    if len(data) >= 4:
        attempts.append(-zlib.MAX_WBITS)
    for wbits in attempts:
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data, max_out)
        except Exception:
            continue
        if not out:
            continue
        if len(out) >= max_out and (d.unconsumed_tail or not d.eof):
            raise _Oversized()
        return out
    return None


def _decoded_text(decode, raw: str) -> str | None:
    '''Dekodiert raw (Hex, Base64, Base32) und macht lesbaren Text daraus. Passt raw nicht
    zur Kodierung: None. _Oversized geht bewusst durch (sperrt den Aufruf).'''
    try:
        data = decode(raw)
    except ValueError:              # binascii.Error ist ein ValueError
        return None
    return _try_decode_to_printable_text(data)


def _try_decode_to_printable_text(data: bytes) -> str | None:
    decompressed = _decompress_up_to_1mb(data)
    b = decompressed if decompressed is not None else data
    if not b:
        return None
    try:
        t = b.decode("utf-8")
        if _is_predominantly_printable(t):
            return t
    except Exception:
        pass
    try:
        t = b.decode("latin-1")
        if _is_predominantly_printable(t):
            return t
    except Exception:
        pass
    return None


def check_egress_content(args: Any, state: EgressState | None = None) -> tuple[bool, str]:
    """Prüft Argumente eines ausgehenden Aufrufs gegen die Fingerabdrücke des Gesprächs.
    Liefert (True, 'content_match:<kategorie>') bei Treffer, sonst (False, '')."""
    if state is None:
        state = get_current_egress_state()
    if not state.marked:
        return False, ""

    try:
        raw_strings = _flatten_strings(args)
        total_size = sum(len(s.encode("utf-8", errors="ignore")) for s in raw_strings)
        if total_size > 200_000:
            return True, "content_match:too_large"

        matched_kinds: list[str] = []
        has_encoded_blob: bool = False
        seen_variants: set[str] = set()
        current_queue: list[str] = [s for s in raw_strings if s]

        priority = {"iban": 0, "email": 1, "id": 2, "phrase": 3}

        for depth in range(4):
            if not current_queue:
                break
            next_queue: list[str] = []

            for s in current_queue:
                if s in seen_variants:
                    continue
                if len(seen_variants) >= 200:
                    return True, "content_match:too_large"
                seen_variants.add(s)

                if state.fingerprints:
                    for h, cand_kind in extract_sensitive_hashes(s).items():
                        if h in state.fingerprints:
                            kind = state.fingerprint_kinds.get(h, cand_kind)
                            matched_kinds.append(kind)

                if depth >= 3:
                    continue

                for v in _decode_variants(s):
                    if v != s and v not in seen_variants:
                        next_queue.append(v)

                norm = unicodedata.normalize("NFKC", s)
                if norm != s and norm not in seen_variants:
                    next_queue.append(norm)

                for m in _SPACED_ID_RE.finditer(s):
                    compact_id = m.group(1).replace(m.group(2), "")
                    if compact_id not in seen_variants:
                        next_queue.append(compact_id)

                compact_s = _SPACED_ID_RE.sub(lambda m: m.group(1).replace(m.group(2), ""), s)
                if compact_s != s and compact_s not in seen_variants:
                    next_queue.append(compact_s)

                rot = codecs.encode(s, "rot_13")
                if rot != s and rot not in seen_variants:
                    next_queue.append(rot)

                rev = s[::-1]
                if rev != s and rev not in seen_variants:
                    next_queue.append(rev)

                for pat in _FORMATTED_HEX_RES:
                    for m in pat.finditer(s):
                        clean_hex = _HEX_CLEAN_RE.sub("", m.group(0))
                        txt = None
                        if len(clean_hex) >= 16 and len(clean_hex) % 2 == 0:
                            txt = _decoded_text(bytes.fromhex, clean_hex)
                        if txt is not None:
                            if txt not in seen_variants:
                                next_queue.append(txt)
                        elif _looks_random(clean_hex, pure_hex=True):
                            has_encoded_blob = True

                for m in _CONTIGUOUS_TOKEN_RE.finditer(s):
                    tok = m.group(0)
                    clean_hex = tok.rstrip("=")
                    is_pure_hex = bool(re.fullmatch(r"[0-9a-fA-F]+", clean_hex))
                    blob_candidate = _looks_random(tok, pure_hex=is_pure_hex)
                    decoded_any = False

                    texts: list[str | None] = []
                    if is_pure_hex and len(clean_hex) % 2 == 0:
                        texts.append(_decoded_text(bytes.fromhex, clean_hex))

                    clean_b64 = tok.rstrip("=")
                    if len(clean_b64) % 4 != 1:
                        pad_len = (4 - len(clean_b64) % 4) % 4
                        std_b64 = (clean_b64 + "=" * pad_len).replace("-", "+").replace("_", "/")
                        texts.append(_decoded_text(
                            lambda v: base64.b64decode(v, validate=True), std_b64))

                    clean_b32 = tok.rstrip("=").upper()
                    if re.fullmatch(r"[A-Z2-7]+", clean_b32) and len(clean_b32) % 8 in (0, 2, 4, 5, 7):
                        padded_b32 = clean_b32 + "=" * ((8 - len(clean_b32) % 8) % 8)
                        texts.append(_decoded_text(
                            lambda v: base64.b32decode(v, casefold=True), padded_b32))

                    for txt in texts:
                        if txt is not None:
                            decoded_any = True
                            if txt not in seen_variants:
                                next_queue.append(txt)

                    if not decoded_any and blob_candidate:
                        has_encoded_blob = True

            if matched_kinds:
                matched_kinds.sort(key=lambda k: priority.get(k, 99))
                return True, f"content_match:{matched_kinds[0]}"

            current_queue = next_queue

        if has_encoded_blob:
            return True, "content_match:encoded_blob"

        return False, ""

    except _Oversized:
        return True, "content_match:too_large"
    except Exception as e:
        log.warning(f"check_egress_content failed: {e}", exc_info=True)
        if state.marked:
            return True, "content_match:check_failed"
        return False, ""


def _log_and_audit_block(tool_name: str, conversation_key: str, reason: str) -> None:
    """Schreibt genau eine Zeile ins Log und ins Audit, OHNE vertrauliche Inhalte."""
    log.warning(
        f"Egress-Guard hat Zugriff nach aussen gesperrt: "
        f"tool={tool_name} conversation={conversation_key} reason={reason}"
    )
    try:
        from rag_backend.action_engine.audit import write_audit_log
        from rag_backend.action_engine.base_tool import CURRENT_USER_ID

        uid = CURRENT_USER_ID.get()
        if uid is None:
            try:
                from rag_backend.agent import request_origin_var
                orig = request_origin_var.get() or {}
                uid = orig.get("user_id")
            except Exception:
                uid = None

        input_hash = hashlib.sha256(f"{tool_name}:{conversation_key}:{reason}".encode("utf-8")).hexdigest()[:16]
        details_json = json.dumps(
            {"conversation": conversation_key, "reason": reason},
            ensure_ascii=False,
        )
        write_audit_log(
            tool=tool_name,
            tool_class="egress",
            input_hash=input_hash,
            output_hash=None,
            status="egress_blocked",
            latency_ms=0,
            user_id=uid if isinstance(uid, int) else None,
            details=details_json,
        )
    except Exception as e:
        log.debug(f"Egress-Guard Audit-Eintrag nicht schreibbar: {e}")


async def _request_egress_confirmation(tool_name: str, conversation_key: str) -> bool:
    """Holt im Modus 'confirm' (bei aktiver Action-Engine) eine Nutzerfreigabe über
    confirmation_store ein, ohne Inhalte in die Beschreibung zu leaken."""
    try:
        from rag_backend.action_engine.base_tool import (
            CURRENT_TASK,
            CURRENT_USER_ID,
            _dispatch_confirmation_event,
        )
        from rag_backend.action_engine.confirmation import (
            ConfirmationStatus,
            confirmation_store,
        )

        task = CURRENT_TASK.get() or {}
        if not task:
            try:
                from rag_backend.agent import request_origin_var
                orig = request_origin_var.get() or {}
                task = {
                    "id": orig.get("task_id"),
                    "text": orig.get("task_text") or "",
                    "channel": orig.get("channel"),
                }
            except Exception:
                task = {}

        # Freigeben kann nur das ausloesende Konto ueber die Karte im Chat. Fehlt
        # eines von beiden, bleibt der Weg zu (wie im Modus 'block').
        requested_by = CURRENT_USER_ID.get()
        if task.get("channel") != "dashboard" or requested_by is None:
            return False
        req = confirmation_store.create(
            tool_name=tool_name,
            tool_class="write",
            parameters={"conversation": conversation_key},
            impact_description=(
                f"Zugriff nach außen über '{tool_name}' in einem Gespräch, "
                "das zuvor vertrauliche Daten gelesen hat."
            ),
            requested_by=requested_by,
            task_id=task.get("id"),
            task_scope=False,
            task_description=task.get("text") or "",
        )
        await _dispatch_confirmation_event("pending", req)
        resolved = await confirmation_store.wait_for_resolution(req.id)
        status_text = resolved.status.value if resolved else "timeout"
        await _dispatch_confirmation_event("resolved", req, status=status_text)
        return bool(resolved and resolved.status == ConfirmationStatus.APPROVED)
    except Exception as e:
        log.warning(f"Egress-Guard Freigabe-Abfrage fehlgeschlagen ({tool_name}): {e}")
        return False


def get_egress_mode() -> str:
    """Liest EGRESS_AFTER_SENSITIVE: 'block' (Standard) oder 'allow'. Jeder andere
    Wert, auch das fruehere 'confirm', sperrt."""
    raw = os.getenv("EGRESS_AFTER_SENSITIVE", "block").strip().lower()
    return "allow" if raw == "allow" else "block"


async def check_egress_allowed(
    tool_name: str,
    args: Any = None,
    *,
    delegate_confirm_to_caller: bool = False,
    force_confirm: bool = False,
) -> str | None:
    """Prüft, ob ein Weg nach außen im aktuellen Gespräch erlaubt ist.

    Rückgabe:
      - None, wenn der Aufruf erlaubt ist.
      - Klartext-Meldung für das Modell, wenn der Aufruf gesperrt wurde.
    """
    state = get_current_egress_state()
    if not state.marked:
        return None

    # 1. Inhaltsprüfung (1d) läuft IMMER in markierten Gesprächen, auch bei EGRESS_AFTER_SENSITIVE=allow!
    matched, match_reason = check_egress_content(args, state=state)
    if matched:
        _log_and_audit_block(tool_name, state.session_key, match_reason)
        return CONTENT_BLOCK_MESSAGE

    # 2. Wenn wir uns bereits innerhalb eines vom äußeren Tool geprüften Scopes befinden
    #    (z.B. _search_and_read innerhalb von web_search), genügt die Inhaltsprüfung oben.
    if is_in_checked_egress_scope() and not force_confirm:
        return None

    mode = get_egress_mode()

    if mode == "block":
        _log_and_audit_block(tool_name, state.session_key, "conversation_marked_sensitive")
        return BLOCK_MESSAGE

    # mode == "allow"
    if state.fingerprints_incomplete:
        # Die Inhaltspruefung kennt nicht alles Gelesene: lieber sperren.
        _log_and_audit_block(tool_name, state.session_key, "fingerprints_incomplete")
        return BLOCK_MESSAGE

    if force_confirm:
        if delegate_confirm_to_caller:
            return None
        approved = await _request_egress_confirmation(tool_name, state.session_key)
        if not approved:
            _log_and_audit_block(tool_name, state.session_key, "mission_auto_approve_blocked")
            return CONFIRM_DENIED_MESSAGE

    return None
