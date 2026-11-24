import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Konfiguration und globaler Zustand
_lock = threading.Lock()
_spool_lock = threading.Lock()
_initialized = False

_siem_mode: str = "off"
_siem_format: str = "json"
_syslog_host: str = ""
_syslog_port: int = 514
_syslog_protocol: str = "udp"
_siem_hostname: str = ""
_cached_version: str = "0"

_siem_spool_dir: str = "/app/data/siem_spool"
_siem_spool_max_mb: float = 50.0
_custom_proc_id: str | None = None

_MAX_QUEUE_SIZE = 10000
_queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
_dropped_count: int = 0
_last_drop_warning: float = 0.0

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_tcp_socket: socket.socket | None = None
#--- Pause nach einem Sendefehler (alle Modi, nicht nur TCP): bis _next_reconnect
#--- gehen neue Eintraege in den Spool, danach zuerst der Spool, dann die Queue.
_tcp_backoff: float = 1.0
_next_reconnect: float = 0.0

#--- Dieser Prozess hat seit der letzten Nachlieferung in seinen Spool geschrieben.
#--- Dateien anderer (abgestuerzter) Prozesse sucht der Sender nur alle paar Sekunden.
_spool_pending: bool = False
_last_spool_scan: float = 0.0
_SPOOL_SCAN_INTERVAL = 5.0


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
    msg = {
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
    if "id" in record and record["id"] is not None:
        msg["id"] = record["id"]
    return msg


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

    if "id" in record and record["id"] is not None:
        ext_parts.append(f"externalId={_cef_escape_value(record['id'])}")

    return header + " ".join(ext_parts)


def _get_process_id() -> str:
    '''Liefert die Kennung des aktuellen Prozesses (standardmaessig die PID).'''
    global _custom_proc_id
    if _custom_proc_id is not None:
        return _custom_proc_id
    return str(os.getpid())


def _is_pid_alive(pid_str: str) -> bool:
    '''Prueft, ob ein Prozess anhand seiner PID noch laeuft.'''
    try:
        pid = int(pid_str)
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                kernel32.CloseHandle(h)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (ValueError, TypeError):
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _ensure_record_id(record: dict) -> str:
    '''Stellt sicher, dass der Datensatz eine eindeutige Kennung hat, an der das SIEM
    Doppelte erkennt. Audit-Eintraege haben sie schon: ihre HMAC-Signatur (verkettet,
    also je Eintrag verschieden). Nur ohne Signatur bekommt der Eintrag eine "id".'''
    for key in ("id", "hmac"):
        val = record.get(key)
        if val is not None and str(val).strip():
            return str(val)
    new_id = uuid.uuid4().hex
    record["id"] = new_id
    return new_id


def _ensure_spool_dir() -> Path:
    '''Erstellt das Spool-Verzeichnis mit Dateirechten 0700 falls nicht vorhanden.'''
    p = Path(_siem_spool_dir)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except Exception:
        pass
    return p


def _get_spool_total_bytes(spool_dir: Path) -> int:
    '''Berechnet den gesamten Speicherplatz aller Spool-Dateien in Bytes.'''
    if not spool_dir.exists():
        return 0
    total = 0
    try:
        for entry in os.scandir(spool_dir):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _spool_record(record: dict) -> bool:
    '''Schreibt einen nicht zugestellten Eintrag in die Spool-Datei.

    Gibt True zurueck, wenn erfolgreich gespeichert.
    Gibt False zurueck, wenn das Spool-Limit erreicht ist (Eintrag verloren).
    '''
    global _spool_pending
    if _siem_mode == "off":
        return False

    _ensure_record_id(record)

    with _spool_lock:
        try:
            spool_dir = _ensure_spool_dir()
            line = json.dumps(record, ensure_ascii=False) + "\n"
            line_bytes = line.encode("utf-8")
            max_bytes = int(_siem_spool_max_mb * 1024 * 1024)

            current_size = _get_spool_total_bytes(spool_dir)
            if current_size + len(line_bytes) > max_bytes:
                _count_dropped("SIEM-Spool-Obergrenze erreicht")
                return False

            pid = _get_process_id()
            file_path = spool_dir / f"spool_{pid}.jsonl"

            # Dateirechte 0600 beim Erstellen atomar setzen
            fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line_bytes)
            finally:
                os.close(fd)

            try:
                os.chmod(file_path, 0o600)
            except Exception:
                pass

            _spool_pending = True
            return True
        except Exception as e:
            log.warning(f"Fehler beim Schreiben in Spool: {e}")
            _count_dropped(f"Spool-Schreibfehler ({e})")
            return False


def _keep_remaining_lines(file_path: Path, remaining_lines: list[str]) -> None:
    '''Schreibt die noch nicht zugestellten Zeilen an Ort und Stelle zurueck.

    Name und Aenderungszeit bleiben, damit die Datei ihren Platz in der Reihenfolge
    behaelt (aelteste zuerst). Scheitert das Zurueckschreiben, bleibt die Datei, wie
    sie war: Bereits gesendete Zeilen gehen dann ein zweites Mal raus (mindestens
    einmal, nie verloren).'''
    tmp = file_path.with_name(file_path.name + ".part")
    try:
        mtime = file_path.stat().st_mtime
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            for line in remaining_lines:
                os.write(fd, line.encode("utf-8") if isinstance(line, str) else line)
        finally:
            os.close(fd)
        os.replace(tmp, file_path)
        os.utime(file_path, (mtime, mtime))
    except Exception as e:
        log.warning(f"Fehler beim Sichern verbleibender Spool-Zeilen: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _drain_spool() -> bool:
    '''Liefert Spool-Eintraege an das SIEM nach (aelteste zuerst).

    Jede Datei gehoert genau einem Prozess (Name mit seiner PID). Nachgeliefert
    werden nur eigene Dateien und solche abgestuerzter Prozesse, die per atomarem
    Umbenennen uebernommen wurden; zwei Prozesse senden so nie dieselbe Datei. Dateien
    eines anderen, noch laufenden Prozesses (etwa des MCP-Dateiservers) liefert der
    selbst nach; sie halten diesen Prozess nicht auf.

    Rueckgabe:
        True: Alles Uebernommene zugestellt (oder nichts zu tun).
        False: Senden fehlgeschlagen (SIEM noch nicht erreichbar).
    '''
    global _spool_pending
    if _siem_mode == "off":
        return True

    spool_dir = Path(_siem_spool_dir)
    if not spool_dir.exists():
        return True

    my_pid = _get_process_id()

    # 1. Eigene aktive Spool-Datei rotieren, falls vorhanden und nicht leer
    with _spool_lock:
        _spool_pending = False
        active_file = spool_dir / f"spool_{my_pid}.jsonl"
        if active_file.exists():
            try:
                if active_file.stat().st_size > 0:
                    ts_ms = int(time.time() * 1000)
                    token = uuid.uuid4().hex[:6]
                    ready_target = spool_dir / f"drain_{my_pid}_{ts_ms}_{token}.jsonl"
                    os.rename(active_file, ready_target)
            except OSError:
                pass

    # 2. Kandidaten-Dateien fuer Nachlieferung suchen und atomar uebernehmen
    my_drain_files: list[Path] = []

    try:
        entries = list(os.scandir(spool_dir))
    except Exception:
        entries = []

    for entry in entries:
        if not (entry.is_file() and entry.name.endswith(".jsonl")):
            continue
        fname = entry.name
        fpath = Path(entry.path)

        # Eigene bereits beanspruchte Drain-Dateien (z. B. vor Neustart)
        if fname.startswith(f"drain_{my_pid}_"):
            my_drain_files.append(fpath)
            continue

        # Drain-Dateien eines anderen Prozesses
        if fname.startswith("drain_"):
            parts = fname.split("_")
            other_pid = parts[1] if len(parts) > 1 else ""
            if not _is_pid_alive(other_pid):
                ts_ms = int(time.time() * 1000)
                token = uuid.uuid4().hex[:6]
                claimed_target = spool_dir / f"drain_{my_pid}_{ts_ms}_{token}.jsonl"
                try:
                    os.rename(fpath, claimed_target)
                    my_drain_files.append(claimed_target)
                except OSError:
                    pass
            continue

        # Spool-Dateien anderer Prozesse oder verwaiste Spool-Dateien
        if fname.startswith("spool_") and fname != f"spool_{my_pid}.jsonl":
            other_pid = fname[len("spool_"):-len(".jsonl")]
            if not _is_pid_alive(other_pid):
                ts_ms = int(time.time() * 1000)
                token = uuid.uuid4().hex[:6]
                claimed_target = spool_dir / f"drain_{my_pid}_{ts_ms}_{token}.jsonl"
                try:
                    os.rename(fpath, claimed_target)
                    my_drain_files.append(claimed_target)
                except OSError:
                    pass
            continue

    if not my_drain_files:
        return True

    # 3. Aelteste zuerst: nach Dateialter (mtime) sortieren
    def _file_sort_key(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    my_drain_files.sort(key=_file_sort_key)

    # 4. Dateien nacheinander abarbeiten
    for drain_file in my_drain_files:
        if not drain_file.exists():
            continue

        try:
            with open(drain_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue

        for i, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                record = json.loads(line_str)
            except Exception:
                continue

            try:
                _dispatch_record(record)
            except Exception:
                # Zustellung fehlgeschlagen: Rest (samt dieser Zeile) zuruecksichern
                _keep_remaining_lines(drain_file, lines[i:])
                return False

        try:
            os.remove(drain_file)
        except OSError:
            pass

    return True


def get_spool_files() -> list[Path]:
    '''Gibt alle vorhandenen Spool- und Drain-Dateien zurueck (fuer Tests).'''
    spool_dir_str = _siem_spool_dir if _initialized else os.getenv("AUDIT_SIEM_SPOOL_DIR", "/app/data/siem_spool").strip()
    p = Path(spool_dir_str)
    if not p.exists():
        return []
    try:
        return [Path(e.path) for e in os.scandir(p) if e.is_file() and e.name.endswith(".jsonl")]
    except Exception:
        return []


def get_spool_records() -> list[dict]:
    '''Liest alle aktuell im Spool liegenden Eintraege (fuer Tests).'''
    records = []
    for fp in get_spool_files():
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if line_s:
                        records.append(json.loads(line_s))
        except Exception:
            pass
    return records


def _init_siem() -> None:
    '''Liest Umgebungsvariablen ein und startet bei Bedarf den Worker-Thread.'''
    global _initialized, _siem_mode, _siem_format, _syslog_host, _syslog_port
    global _syslog_protocol, _siem_hostname, _cached_version, _worker_thread
    global _siem_spool_dir, _siem_spool_max_mb

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

        _siem_spool_dir = os.getenv("AUDIT_SIEM_SPOOL_DIR", "/app/data/siem_spool").strip()
        try:
            _siem_spool_max_mb = float(os.getenv("AUDIT_SIEM_SPOOL_MAX_MB", "50").strip())
        except ValueError:
            _siem_spool_max_mb = 50.0

        if _siem_mode in ("stdout", "syslog"):
            #--- Ohne Spool-Ordner geht die Weiterleitung trotzdem; nur was nicht
            #--- zugestellt wird, zaehlt dann als verloren.
            try:
                _ensure_spool_dir()
            except Exception as e:
                log.warning(f"SIEM-Spool-Ordner {_siem_spool_dir} nicht nutzbar: {e}")
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
        header = f"<{syslog_pri}>1 {iso_z} {_syslog_token(_siem_hostname, 255)} argus - {event_name} - "
        packet = header + nachricht

        if _syslog_protocol == "tcp":
            payload = packet.encode("utf-8")
            frame = f"{len(payload)} ".encode("ascii") + payload
            _send_tcp(frame, _syslog_host, _syslog_port)
        else:
            # UDP: ein Datagramm je Nachricht. UDP meldet keine Verluste; das ist so.
            # Was unterwegs verloren geht, sieht Argus nicht und liefert es nicht nach;
            # in den Spool kommt nur, was schon beim Senden scheitert. Wer nichts
            # verlieren darf, nimmt TCP.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet.encode("utf-8"), (_syslog_host, _syslog_port))
            finally:
                sock.close()


def _mark_failure() -> None:
    '''Sendefehler: Pause bis _next_reconnect, mit wachsendem Abstand (hoechstens 30 s).
    _send_tcp setzt die Pause selbst; dann bleibt es bei dieser.'''
    global _tcp_backoff, _next_reconnect
    now = time.time()
    if _next_reconnect <= now:
        _next_reconnect = now + _tcp_backoff
        _tcp_backoff = min(_tcp_backoff * 2.0, 30.0)


def _worker_loop() -> None:
    '''Hintergrund-Thread zur asynchronen Auslieferung an das SIEM.'''
    global _last_spool_scan, _tcp_backoff
    while not _stop_event.is_set():
        # 1. SIEM gerade nicht erreichbar: neue Eintraege hinten an den Spool, damit
        #    die Reihenfolge stimmt; gesendet wird erst nach der Pause wieder.
        if time.time() < _next_reconnect:
            try:
                record = _queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                _spool_record(record)
            finally:
                _queue.task_done()
            continue

        # 2. Spool nachliefern, bevor neue Eintraege aus der Queue gehen: eigene
        #    Eintraege sofort, Dateien abgestuerzter Prozesse beim naechsten Rundgang.
        now = time.time()
        if _spool_pending or now - _last_spool_scan >= _SPOOL_SCAN_INTERVAL:
            _last_spool_scan = now
            if not _drain_spool():
                _mark_failure()
                continue

        # 3. Neue Eintraege aus der Queue
        try:
            record = _queue.get(timeout=0.1)
        except queue.Empty:
            continue

        try:
            _dispatch_record(record)
            _tcp_backoff = 1.0
        except Exception:
            _mark_failure()
            _spool_record(record)
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
            _spool_record(record)
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
    global _siem_spool_dir, _siem_spool_max_mb, _custom_proc_id
    global _spool_pending, _last_spool_scan

    if flush:
        start_wait = time.time()
        while not _queue.empty() and (time.time() - start_wait < timeout):
            time.sleep(0.02)

    stop(timeout=timeout)

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
        _spool_pending = False
        _last_spool_scan = 0.0
        _siem_spool_dir = os.getenv("AUDIT_SIEM_SPOOL_DIR", "/app/data/siem_spool").strip()
        try:
            _siem_spool_max_mb = float(os.getenv("AUDIT_SIEM_SPOOL_MAX_MB", "50").strip())
        except ValueError:
            _siem_spool_max_mb = 50.0
        _custom_proc_id = None
