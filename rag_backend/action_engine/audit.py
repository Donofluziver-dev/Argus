import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_HMAC_KEY: bytes | None = None


def _get_hmac_key() -> bytes:
    global _HMAC_KEY
    if _HMAC_KEY is None:
        # Dedizierter HMAC-Key, kein Rueckfall auf den Fernet-Key: der ist
        # welt-lesbar und mehrfach verwendet.
        from rag_backend.crypto_utils import load_secret
        key = load_secret("AUDIT_HMAC_KEY")
        if not key:
            raise RuntimeError(
                "AUDIT_HMAC_KEY nicht verfuegbar -- setup/init_stack.py erneut ausfuehren "
                "(legt API_Tokens/audit_hmac_key.txt an)."
            )
        _HMAC_KEY = key.encode()
    return _HMAC_KEY


def _compute_hmac(payload: dict) -> str:
    key = _get_hmac_key()
    msg = json.dumps(payload, sort_keys=True, default=str).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _get_anchor_path() -> Path:
    raw = os.getenv("AUDIT_ANCHOR_PATH", "/app/data/audit_anchor.json")
    return Path(raw)


def _write_anchor(count: int, last_sig: str) -> None:
    anchor_path = _get_anchor_path()
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"count": count, "last_signature": last_sig}
    sig = _compute_hmac(payload)
    data = {
        "count": count,
        "last_signature": last_sig,
        "hmac": sig,
    }
    tmp_path = anchor_path.with_name(f"{anchor_path.name}.tmp.{uuid.uuid4().hex}")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, anchor_path)


def write_audit_log(
    tool: str,
    tool_class: str,
    input_hash: str,
    output_hash: str | None,
    status: str,
    latency_ms: int | None = None,
    user_id: int | None = None,
    details: str | None = None,
) -> bool:
    try:
        from rag_backend.database import SessionLocal
        from rag_backend.models import AuditLog

        # ts als naive-UTC: die Spalte ist tz-naiv, sonst bricht +00:00 die Pruefung.
        ts = datetime.now(timezone.utc).replace(tzinfo=None)

        with SessionLocal() as db:
            # Hash-Chain: die Signatur der letzten Zeile geht in den Payload dieser
            # Zeile ein. Aendern/Loeschen/Umsortieren bricht die Kette.
            # Advisory-Lock, weil Hauptprozess und MCP-Subprozess in dieselbe Kette
            # schreiben; SQLite kennt die Funktion nicht -> still ignorieren.
            from sqlalchemy import text as _sa_text
            try:
                db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"), {"k": 0x4155444954})
            except Exception:
                pass
            last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
            prev_sig = last.hmac_signature if (last and last.hmac_signature) else ""

            payload = {
                "ts": ts.isoformat(timespec="microseconds"),
                "tool": tool,
                "tool_class": tool_class,
                "input_hash": input_hash,
                "output_hash": output_hash or "",
                "status": status,
                "latency_ms": latency_ms or 0,
                "user_id": user_id or 0,
                "details": details or "",
                "prev": prev_sig,
            }
            signature = _compute_hmac(payload)

            db.add(AuditLog(
                ts=ts,
                user_id=user_id,
                tool=tool,
                tool_class=tool_class,
                input_hash=input_hash,
                output_hash=output_hash,
                status=status,
                latency_ms=latency_ms,
                hmac_signature=signature,
                details=details,
            ))
            #--- Anker NOCH unter dem Advisory-Lock schreiben (er endet mit dem
            #--- Commit): sonst koennen zwei Schreiber Zaehler und Endsignatur
            #--- vertauschen, und die Pruefung meldet eine Manipulation, die es
            #--- nicht gab. Ein Ankerfehler darf den Eintrag selbst nie kosten.
            db.flush()
            total_count = db.query(AuditLog).count()
            try:
                _write_anchor(total_count, signature)
            except Exception as anchor_err:
                log.error(f"audit_log Anker nicht schreibbar: {anchor_err}")
            db.commit()

        # Weitergabe an SIEM (optional, blockiert nicht)
        try:
            from rag_backend import siem
            siem.forward({
                "ts": ts.isoformat(timespec="microseconds"),
                "tool": tool,
                "tool_class": tool_class,
                "status": status,
                "latency_ms": latency_ms,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "user_id": user_id,
                "details": details,
                "hmac": signature,
            })
        except Exception as siem_err:
            log.warning(f"audit_log SIEM-Weiterleitung fehlgeschlagen: {siem_err}")

        # Zusaetzlich als JSONL auf dem Host-Volume, ein File pro Tag.
        try:
            audit_dir = Path("/app/evals/Action_Audit")
            audit_dir.mkdir(parents=True, exist_ok=True)
            daily_file = audit_dir / f"audit_{ts.strftime('%Y-%m-%d')}.jsonl"
            line = json.dumps({
                "ts": ts.isoformat(timespec="microseconds"),
                "tool": tool, "tool_class": tool_class,
                "status": status, "latency_ms": latency_ms,
                "input_hash": input_hash, "output_hash": output_hash,
                "user_id": user_id, "details": details,
                "hmac": signature,
            }, ensure_ascii=False)
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e2:
            log.warning(f"audit_log Datei-Persistierung fehlgeschlagen: {e2}")
        return True
    except Exception as e:
        log.error(f"audit_log Schreibfehler: {e}")
        return False


def verify_audit_chain() -> dict:
    # Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
    '''Rechnet die HMAC-Hash-Chain ueber alle Audit-Zeilen (id aufsteigend) neu und
    meldet, ob die Kette intakt ist: {ok, checked, broken_id, reason}. broken_id ist die
    erste Zeile, deren Signatur sich nicht reproduzieren laesst (Manipulation/Luecke).
    Vergleicht zusaetzlich Zeilenanzahl und End-Signatur mit dem signierten Anker,
    um ein Abschneiden/Loeschen am Ende zu erkennen.'''
    try:
        from rag_backend.database import SessionLocal
        from rag_backend.models import AuditLog
    except Exception as e:
        return {"ok": False, "checked": 0, "broken_id": None, "error": str(e), "reason": str(e)}

    anchor_path = _get_anchor_path()
    anchor_data = None
    if anchor_path.exists():
        try:
            anchor_data = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor_payload = {
                "count": anchor_data.get("count"),
                "last_signature": anchor_data.get("last_signature"),
            }
            if _compute_hmac(anchor_payload) != anchor_data.get("hmac"):
                return {
                    "ok": False,
                    "checked": 0,
                    "broken_id": None,
                    "reason": "Anker-Signatur ungültig (Manipulation am Anker erkannt)",
                }
        except Exception as e:
            return {
                "ok": False,
                "checked": 0,
                "broken_id": None,
                "reason": f"Anker-Datei beschädigt oder unlesbar: {e}",
            }

    prev_sig = ""
    checked = 0
    with SessionLocal() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        if anchor_data is None:
            if len(rows) > 0:
                return {
                    "ok": False,
                    "checked": 0,
                    "broken_id": None,
                    "reason": "Anker-Datei fehlt, obwohl Einträge in der Audit-Tabelle vorhanden sind",
                }
            return {"ok": True, "checked": 0, "broken_id": None}

        for r in rows:
            payload = {
                "ts": r.ts.isoformat(timespec="microseconds") if r.ts else "",
                "tool": r.tool,
                "tool_class": r.tool_class,
                "input_hash": r.input_hash,
                "output_hash": r.output_hash or "",
                "status": r.status,
                "latency_ms": r.latency_ms or 0,
                "user_id": r.user_id or 0,
                "details": r.details or "",
                "prev": prev_sig,
            }
            if _compute_hmac(payload) != r.hmac_signature:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_id": r.id,
                    "reason": f"Hash-Kette in Zeile {r.id} unterbrochen",
                }
            prev_sig = r.hmac_signature
            checked += 1

        expected_count = anchor_data.get("count", 0)
        expected_last_sig = anchor_data.get("last_signature", "")
        if checked != expected_count:
            return {
                "ok": False,
                "checked": checked,
                "broken_id": None,
                "reason": f"Anzahl der Einträge weicht vom Anker ab (erwartet {expected_count}, vorhanden {checked})",
            }
        if prev_sig != expected_last_sig:
            return {
                "ok": False,
                "checked": checked,
                "broken_id": None,
                "reason": f"Letzte Signatur weicht vom Anker ab (erwartet {expected_last_sig}, vorhanden {prev_sig})",
            }

    return {"ok": True, "checked": checked, "broken_id": None}
