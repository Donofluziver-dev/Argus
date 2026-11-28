'''Koeder-Tokens: Frühwarnung gegen Datenabfluss.

Ein Koeder ist ein erfundener Schluessel, der nirgends gebraucht wird -- z. B. in
einer Datei C:\\Argus_Workspace\\zugangsdaten.txt oder als Zeile in einer .env.
Liest ein per Prompt-Injection gesteuerter Agent ihn und versucht, ihn nach
aussen zu geben (Cloud-Anfrage, Websuche, URL, Host-Befehl), wird der Vorgang
blockiert, als Fehler protokolliert und im Audit-Log vermerkt.

Konfiguration: CANARY_TOKENS="koeder1,koeder2" (Komma-getrennt, je >= 12 Zeichen).
'''
import base64
import html
import logging
import os
import re
from urllib.parse import unquote, unquote_plus

log = logging.getLogger(__name__)

_MIN_LEN = 12


def canary_tokens() -> list[str]:
    raw = os.getenv("CANARY_TOKENS", "")
    #--- Zu kurze Koeder tauchen zufaellig in normalem Text auf.
    return [t.strip() for t in raw.split(",") if len(t.strip()) >= _MIN_LEN]


_SEPARATORS_RE = re.compile(r"[\s.\-_,;:|/\\]+")


def _base64_cores(raw: bytes) -> set[str]:
    """Base64-Darstellungen des Koeders, wie sie mitten in einer laengeren
    Base64-Folge stehen: je nach Versatz (0, 1 oder 2 Bytes davor) andere Zeichen.
    Nur die Zeichen, die allein vom Koeder bestimmt sind."""
    out = set()
    for k in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            enc = encode(b"\0" * k + raw).decode("ascii")
            lo = -(-8 * k // 6)
            hi = (8 * (k + len(raw))) // 6
            core = enc[lo:hi]
            if len(core) >= _MIN_LEN:
                out.add(core)
    return out


def _token_patterns(token: str) -> tuple[set[str], set[str]]:
    """(ohne Gross-/Kleinschreibung zu vergleichen, genau zu vergleichen)."""
    raw = token.encode("utf-8")
    loose = {token.lower(), token[::-1].lower(), raw.hex()}
    stripped = _SEPARATORS_RE.sub("", token).lower()
    if len(stripped) >= _MIN_LEN:
        loose.add(stripped)
    return loose, _base64_cores(raw)


def _text_variants(text: str) -> set[str]:
    """Der Text, mit aufgeloesten HTML-Entities und URL-Kodierungen (mehrfach
    verschachtelt) und ohne Trennzeichen (Koeder zeichenweise getrennt)."""
    base = html.unescape(text)
    out = {text, base}
    for decode in (unquote, unquote_plus):
        cur = base
        for _ in range(3):
            nxt = decode(cur)
            if nxt == cur:
                break
            cur = nxt
        out.add(cur)
    out |= {_SEPARATORS_RE.sub("", v) for v in list(out)}
    return out


def contains_canary(text: str | None) -> bool:
    """True, wenn ein Koeder im Text steht, auch URL-/HTML-kodiert, als Base64, Hex,
    rueckwaerts oder durch Trennzeichen zerlegt. Ohne Meldung."""
    if not text:
        return False
    tokens = canary_tokens()
    if not tokens:
        return False
    variants = _text_variants(text)
    lowered = [v.lower() for v in variants]
    for token in tokens:
        loose, exact = _token_patterns(token)
        if any(p in v for v in lowered for p in loose):
            return True
        if any(p in v for v in variants for p in exact):
            return True
    return False


def check_canary(text: str | None, where: str) -> bool:
    '''True, wenn ein Koeder im Text steht (siehe contains_canary). Meldet den
    Treffer (ohne den Wert).'''
    if contains_canary(text):
        log.error(f"KOEDER-ALARM: Koeder-Token in ausgehenden Daten ({where}) -- blockiert.")
        _audit(where)
        return True
    return False


def _audit(where: str) -> None:
    '''Treffer ins Audit-Log, damit er in der Verwaltung sichtbar bleibt.

    Fail-open: blockiert ist der Vorgang schon; ein Audit-Problem aendert daran nichts.'''
    try:
        from rag_backend.action_engine.audit import write_audit_log
        write_audit_log(
            tool=where[:100],
            tool_class="egress",
            input_hash="canary",
            output_hash=None,
            status="canary_alarm",
            latency_ms=0,
            user_id=None,
            details=None,
        )
    except Exception as e:
        log.warning(f"Koeder-Alarm nicht ins Audit-Log geschrieben: {e}")
