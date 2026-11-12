import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Konfiguration und globaler Zustand
_lock = threading.Lock()
_initialized = False

_siem_mode: str = "off"
_siem_format: str = "json"
_syslog_host: str = ""
_syslog_port: int = 514
_syslog_protocol: str = "udp"
_siem_hostname: str = ""
_cached_version: str = "0"

_MAX_QUEUE_SIZE = 10000
_queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
_dropped_count: int = 0
_last_drop_warning: float = 0.0

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_tcp_socket: socket.socket | None = None
_tcp_backoff: float = 1.0
_next_reconnect: float = 0.0


def _load_version() -> str:
    '''Liest die Version aus rag_backend/pyproject.toml, sonst "0".'''
    try:
        import tomllib
        base_dir = Path(__file__).resolve().parent
        toml_path = base_dir / "pyproject.toml"
        if not toml_path.exists():
            toml_path = base_dir.parent / "pyproject.toml"
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
                return str(data.get("project", {}).get("version", "0"))
    except Exception:
        pass
    return "0"


def _syslog_token(val: Any, max_len: int) -> str:
    '''Feld fuer den Syslog-Kopf (RFC 5424: druckbares ASCII ohne Leerzeichen).
    Werkzeugnamen und Rechnernamen koennen Leerzeichen oder Umlaute enthalten und
    wuerden den Kopf sonst zerreissen.'''
    s = "".join(c if 33 <= ord(c) <= 126 else "_" for c in str(val or ""))[:max_len]
    return s or "-"


def _out_stream():
    '''Ziel fuer AUDIT_SIEM=stdout. Im stdio-MCP-Server gehoert stdout dem Protokoll;
    dort geht die Zeile nach stderr (landet ebenfalls im Container-Log).'''
    return sys.stderr if os.environ.get("ARGUS_STDOUT_RESERVED") == "1" else sys.stdout


def _cef_escape_header(val: Any) -> str:
    '''Escaped Backslash und Pipe fuer den CEF-Header.'''
    s = str(val if val is not None else "")
    return s.replace("\\", "\\\\").replace("|", "\\|")


def _cef_escape_value(val: Any) -> str:
    '''Escaped Backslash, Gleichheitszeichen und Zeilenumbrueche fuer CEF-Werte.'''
    s = str(val if val is not None else "")
    s = s.replace("\\", "\\\\")
    s = s.replace("=", "\\=")
    s = s.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    return s


def _map_severity(status: str) -> tuple[int, int]:
    '''Ermittelt (cef_severity 0-10, syslog_pri) fuer den gegebenen Status.

    Syslog Facility local4 entspricht numerisch 20.
    PRI = Facility * 8 + Severity:
      canary_alarm -> Alert (1), PRI 161
      locked, denied, egress_blocked, blocked -> Warning (4), PRI 164
      failed, error, exception -> Notice (5), PRI 165
      alles andere -> Info (6), PRI 166
    '''
    s = (status or "").lower()
    if s == "canary_alarm":
        return 10, 161
    elif s in ("locked", "denied", "egress_blocked", "blocked"):
        return 7, 164
    elif s in ("failed", "error", "exception"):
        return 5, 165
    else:
        return 3, 166


def _parse_ts(ts_raw: Any) -> tuple[str, int]:
    '''Konvertiert den Zeitstempel in ISO-8601 UTC mit Z und Epoch-Millisekunden.'''
    if isinstance(ts_raw, datetime):
        dt = ts_raw if ts_raw.tzinfo is not None else ts_raw.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        iso_z = dt_utc.isoformat().replace("+00:00", "Z")
        return iso_z, int(dt_utc.timestamp() * 1000)
    elif isinstance(ts_raw, str) and ts_raw.strip():
        s = ts_raw.strip()
        try:
            if s.endswith("Z"):
                dt = datetime.fromisoformat(s[:-1] + "+00:00")
            elif "+" in s[10:] or "-" in s[10:]:
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
            iso_z = dt_utc.isoformat().replace("+00:00", "Z")
            return iso_z, int(dt_utc.timestamp() * 1000)
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    return now.isoformat().replace("+00:00", "Z"), int(now.timestamp() * 1000)


def _parse_details(details_raw: Any) -> tuple[Any, dict | None]:
    '''Gibt (details_for_json, details_dict_or_none) zurueck.'''
    if details_raw is None:
        return {}, {}
    if isinstance(details_raw, dict):
        return details_raw, details_raw
    if isinstance(details_raw, str):
        if not details_raw.strip():
            return "", None
        try:
            parsed = json.loads(details_raw)
            if isinstance(parsed, dict):
                return parsed, parsed
            return parsed, None
        except Exception:
            return details_raw, None
    return str(details_raw), None


def _format_json(record: dict, iso_z: str, hostname: str, details_val: Any) -> dict:
    return {
        "type": "argus.audit",
        "ts": iso_z,
        "host": hostname,
        "event": record.get("tool") or "",
        "category": record.get("tool_class") or "",
        "status": record.get("status") or "",
        "user_id": record.get("user_id"),
        "details": details_val,
        "input_hash": record.get("input_hash") or "",
        "output_hash": record.get("output_hash") or "",
        "latency_ms": record.get("latency_ms") or 0,
        "hmac": record.get("hmac") or "",
    }


def _format_cef(
    record: dict,
    epoch_ms: int,
    version: str,
    cef_sev: int,
    details_val: Any,
    details_dict: dict | None,
) -> str:
    event = record.get("tool") or ""
    header = (
        f"CEF:0|Argus|Argus|{_cef_escape_header(version)}|"
        f"{_cef_escape_header(event)}|{_cef_escape_header(event)}|{cef_sev}|"
    )

    ext_parts: list[str] = [f"rt={epoch_ms}"]
    user_id = record.get("user_id")
    if user_id is not None:
        ext_parts.append(f"suid={user_id}")

    if details_dict and isinstance(details_dict, dict):
        account = details_dict.get("account")
        if account:
            ext_parts.append(f"suser={_cef_escape_value(account)}")
        ip = details_dict.get("ip")
        if ip:
            ext_parts.append(f"src={_cef_escape_value(ip)}")

    ext_parts.append(f"outcome={_cef_escape_value(record.get('status') or '')}")
    ext_parts.append(f"cat={_cef_escape_value(record.get('tool_class') or '')}")

    if isinstance(details_val, (dict, list)):
        compact_details = json.dumps(details_val, ensure_ascii=False, separators=(',', ':'))
    else:
        compact_details = str(details_val if details_val is not None else "")
    ext_parts.append("cs1Label=details")
    ext_parts.append(f"cs1={_cef_escape_value(compact_details)}")

    hmac_val = str(record.get("hmac") or "")
    ext_parts.append("cs2Label=hmac")
    ext_parts.append(f"cs2={_cef_escape_value(hmac_val)}")

    return header + " ".join(ext_parts)


def _init_siem() -> None:
    '''Liest Umgebungsvariablen ein und startet bei Bedarf den Worker-Thread.'''
    global _initialized, _siem_mode, _siem_format, _syslog_host, _syslog_port
    global _syslog_protocol, _siem_hostname, _cached_version, _worker_thread

    with _lock:
        if _initialized:
            return
        _siem_mode = os.getenv("AUDIT_SIEM", "off").strip().lower()
        _siem_format = os.getenv("AUDIT_SIEM_FORMAT", "json").strip().lower()
        _syslog_host = os.getenv("AUDIT_SYSLOG_HOST", "").strip()
        try:
            _syslog_port = int(os.getenv("AUDIT_SYSLOG_PORT", "514").strip())
        except ValueError:
            _syslog_port = 514
        _syslog_protocol = os.getenv("AUDIT_SYSLOG_PROTOCOL", "udp").strip().lower()
        _siem_hostname = os.getenv("AUDIT_SIEM_HOSTNAME", "").strip() or socket.gethostname()
        _cached_version = _load_version()

        if _siem_mode in ("stdout", "syslog"):
            _stop_event.clear()
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="argus-siem-worker")
            _worker_thread.start()

        _initialized = True


def _send_tcp(frame: bytes, host: str, port: int) -> None:
    '''Sendet Daten ueber eine persistente TCP-Verbindung mit Reconnect-Backoff.'''
    global _tcp_socket, _tcp_backoff, _next_reconnect
    now = time.time()
    if _tcp_socket is None:
        if now < _next_reconnect:
            #--- In der Pause nach einem Fehler: Eintrag gilt als nicht zugestellt.
            raise ConnectionError("SIEM nicht erreichbar, neuer Versuch nach der Pause")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((host, port))
            _tcp_socket = s
            _tcp_backoff = 1.0
            _next_reconnect = 0.0
        except Exception as conn_err:
            if _tcp_socket:
                try:
                    _tcp_socket.close()
                except Exception:
                    pass
                _tcp_socket = None
            _next_reconnect = time.time() + _tcp_backoff
            _tcp_backoff = min(_tcp_backoff * 2.0, 30.0)
            raise conn_err

    try:
        _tcp_socket.sendall(frame)
    except Exception as send_err:
        try:
            if _tcp_socket:
                _tcp_socket.close()
        except Exception:
            pass
        _tcp_socket = None
        _next_reconnect = time.time() + _tcp_backoff
        _tcp_backoff = min(_tcp_backoff * 2.0, 30.0)
        raise send_err


def _dispatch_record(record: dict) -> None:
    '''Formatiert und sendet einen einzelnen Audit-Eintrag.'''
    iso_z, epoch_ms = _parse_ts(record.get("ts"))
    details_val, details_dict = _parse_details(record.get("details"))
    cef_sev, syslog_pri = _map_severity(record.get("status") or "")

    json_msg = _format_json(record, iso_z, _siem_hostname, details_val)

    if _siem_mode == "stdout":
        # stdout: eine Zeile JSON je Eintrag auf sys.stdout (flush), ohne Syslog-Kopf
        line = json.dumps(json_msg, ensure_ascii=False) + "\n"
        out = _out_stream()
        out.write(line)
        out.flush()
        return

    if _siem_mode == "syslog":
        if not _syslog_host:
            return
        if _siem_format == "cef":
            nachricht = _format_cef(record, epoch_ms, _cached_version, cef_sev, details_val, details_dict)
        else:
            nachricht = json.dumps(json_msg, ensure_ascii=False)

        event_name = _syslog_token(record.get("tool"), 32)
        # Syslog-Kopf nach RFC 5424
        header = f"<{syslog_pri}>1 {iso_z} {_syslog_token(_siem_hostname, 255)} argus - {event_name} - "
        packet = header + nachricht

        if _syslog_protocol == "tcp":
            # RFC 6587 Octet-Counting ("<Laenge> <Nachricht>")
            payload = packet.encode("utf-8")
            frame = f"{len(payload)} ".encode("ascii") + payload
            _send_tcp(frame, _syslog_host, _syslog_port)
        else:
            # UDP: ein Datagramm je Nachricht
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet.encode("utf-8"), (_syslog_host, _syslog_port))
            finally:
                sock.close()


def _worker_loop() -> None:
    '''Hintergrund-Thread zur asynchronen Auslieferung an das SIEM.'''
    while not _stop_event.is_set():
        try:
            record = _queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            _dispatch_record(record)
        except Exception as err:
            #--- Nicht zugestellt: mitzaehlen, hoechstens eine Warnung je Minute.
            #--- Massgeblich bleibt das Protokoll in der Datenbank.
            _count_dropped(f"SIEM-Weiterleitung fehlgeschlagen ({err})")
        finally:
            _queue.task_done()


def _count_dropped(reason: str) -> None:
    global _dropped_count, _last_drop_warning
    _dropped_count += 1
    now = time.time()
    if now - _last_drop_warning >= 60.0:
        log.warning(f"{reason}: bisher {_dropped_count} Eintraege nicht zugestellt.")
        _last_drop_warning = now


def forward(record: dict) -> None:
    '''Leitet einen Audit-Eintrag asynchron und non-blocking an das SIEM weiter.'''
    try:
        if not _initialized:
            _init_siem()

        if _siem_mode == "off":
            return

        try:
            _queue.put_nowait(record)
        except queue.Full:
            _count_dropped("SIEM-Warteschlange voll")
    except Exception as e:
        log.warning(f"siem.forward unerwarteter Fehler: {e}")


def get_dropped_count() -> int:
    '''Gibt die Anzahl verworfener Eintraege zurueck (fuer Tests).'''
    return _dropped_count


def stop(timeout: float = 2.0) -> None:
    '''Beendet den Sender-Thread sauber und schliesst bestehende Verbindungen.'''
    global _worker_thread, _tcp_socket
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=timeout)
    _worker_thread = None

    if _tcp_socket:
        try:
            _tcp_socket.close()
        except Exception:
            pass
        _tcp_socket = None


def reset(flush: bool = True, timeout: float = 2.0) -> None:
    '''Setzt die SIEM-Konfiguration und Warteschlange zurueck (fuer Tests).'''
    global _initialized, _dropped_count, _last_drop_warning, _tcp_backoff, _next_reconnect

    if flush:
        # Warten, bis offene Eintraege abgearbeitet sind
        start_wait = time.time()
        while not _queue.empty() and (time.time() - start_wait < timeout):
            time.sleep(0.02)

    stop(timeout=timeout)

    # Verbleibende Warteschlange leeren
    while not _queue.empty():
        try:
            _queue.get_nowait()
            _queue.task_done()
        except Exception:
            break

    with _lock:
        _initialized = False
        _dropped_count = 0
        _last_drop_warning = 0.0
        _tcp_backoff = 1.0
        _next_reconnect = 0.0
