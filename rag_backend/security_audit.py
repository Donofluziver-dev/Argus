import hashlib
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Sensible Schluessel, die niemals im Audit-Protokoll landen duerfen
_FORBIDDEN_KEY_SUBSTRINGS = ("password", "token", "cookie", "secret")


def audit_auth(
    event: str,
    *,
    status: str,
    request: Any = None,
    user_id: int | None = None,
    account: str | None = None,
    account_exists: bool = True,
    details: dict | None = None,
) -> None:
    '''Schreibt Sicherheits- und Konto-Ereignisse in das Audit-Protokoll.

    Wirft niemals eine Ausnahme. Fuer unbekannte Konten wird der Kontoname
    nicht im Klartext gespeichert, sondern nur als Hash in input_hash.
    Passwoerter, Tokens und Cookies werden gefiltert.
    '''
    try:
        from rag_backend.action_engine.audit import write_audit_log

        # Input-Hash ueber den kleingeschriebenen, getrimmten Kontonamen
        if account:
            norm_account = str(account).strip().lower()
            input_hash = hashlib.sha256(norm_account.encode("utf-8")).hexdigest()[:16] if norm_account else ""
        else:
            input_hash = ""

        payload: dict[str, Any] = {}

        # Client-IP ermitteln, falls Request vorhanden
        if request is not None:
            client = getattr(request, "client", None)
            host = getattr(client, "host", None) if client else None
            if host:
                payload["ip"] = host

            # X-Forwarded-For nur auswerten, wenn AUDIT_TRUST_FORWARDED=true
            trust_forwarded = os.getenv("AUDIT_TRUST_FORWARDED", "false").lower() in ("true", "1", "yes")
            if trust_forwarded:
                headers = getattr(request, "headers", None)
                if headers:
                    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
                    if xff:
                        first_hop = xff.split(",")[0].strip()[:64]
                        if first_hop:
                            payload["forwarded_for"] = first_hop

        # Kontoname nur bei existierendem Konto im Klartext aufnehmen
        if account_exists and account:
            payload["account"] = str(account).strip()

        # Zusaetzliche Details aufnehmen, sensible Schluessel filtern
        if details and isinstance(details, dict):
            for k, v in details.items():
                k_lower = str(k).lower()
                if any(bad in k_lower for bad in _FORBIDDEN_KEY_SUBSTRINGS):
                    continue
                payload[k] = v

        # Absicherung: falls details versehentlich ein account bei unbekanntem Nutzer enthielt
        if not account_exists:
            payload.pop("account", None)

        details_json = json.dumps(payload, ensure_ascii=False)

        write_audit_log(
            tool=event,
            tool_class="auth",
            input_hash=input_hash,
            output_hash=None,
            status=status,
            latency_ms=0,
            user_id=user_id,
            details=details_json,
        )
    except Exception as e:
        log.warning(f"audit_auth fehlgeschlagen: {e}")
