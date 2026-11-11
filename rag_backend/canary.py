'''Koeder-Tokens: Frühwarnung gegen Datenabfluss.

Ein Koeder ist ein erfundener Schluessel, der nirgends gebraucht wird -- z. B. in
einer Datei C:\\Argus_Workspace\\zugangsdaten.txt oder als Zeile in einer .env.
Liest ein per Prompt-Injection gesteuerter Agent ihn und versucht, ihn nach
aussen zu geben (Cloud-Anfrage, Websuche, URL, Host-Befehl), wird der Vorgang
blockiert, als Fehler protokolliert und im Audit-Log vermerkt.

Konfiguration: CANARY_TOKENS="koeder1,koeder2" (Komma-getrennt, je >= 12 Zeichen).
'''
import logging
import os

log = logging.getLogger(__name__)

_MIN_LEN = 12


def canary_tokens() -> list[str]:
    raw = os.getenv("CANARY_TOKENS", "")
    #--- Zu kurze Koeder tauchen zufaellig in normalem Text auf.
    return [t.strip() for t in raw.split(",") if len(t.strip()) >= _MIN_LEN]


def check_canary(text: str | None, where: str) -> bool:
    '''True, wenn ein Koeder im Text steht. Meldet den Treffer (ohne den Wert).'''
    if not text:
        return False
    for token in canary_tokens():
        if token in text:
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
