import base64
import os
import tempfile
import pytest

try:
    from calc_sandbox.app import _run, _exec_code, _EXEC_LOCK, sanitize_filename
except ModuleNotFoundError:
    from app import _run, _exec_code, _EXEC_LOCK, sanitize_filename

#--- Aufraeumen zwischen Laeufen (Prozesse beenden, /tmp leeren) gibt es nur im
#--- Sandbox-Abbild. Die Tests dazu laufen dort (CI: docker run mit dem Abbild).
nur_im_container = pytest.mark.skipif(
    os.environ.get("ARGUS_SANDBOX_CONTAINER") != "1",
    reason="nur im Sandbox-Abbild (ARGUS_SANDBOX_CONTAINER=1)",
)


def test_attacks_are_blocked_and_cannot_read_files(tmp_path):
    # Geheimdatei anlegen: ihr Inhalt darf in keiner Ausgabe auftauchen.
    secret_content = "GEHEIM_PROBE_42_NICHT_LESEN"
    secret_file = tmp_path / "probe_secret.txt"
    secret_file.write_text(secret_content, encoding="utf-8")
    p = secret_file.as_posix()

    attacks = [
        'simplify("Symbol(op" + "en(\'/tmp/probe_secret.txt\').read())")',
        'Eq("Symbol(op" + "en(\'/tmp/probe_secret.txt\').read())", 1)',
        'Matrix(["Symbol(op" + "en(\'/tmp/probe_secret.txt\').read())"])',
        'simplify(\'Symbol(_\' + \'_import_\' + \'_("o" + "s").popen("cat /tmp/probe_secret.txt").read())\')',
        f'simplify("Symbol(op" + "en(\'{p}\').read())")',
        f'Eq("Symbol(op" + "en(\'{p}\').read())", 1)',
        f'Matrix(["Symbol(op" + "en(\'{p}\').read())"])',
        f'simplify(\'Symbol(_\' + \'_import_\' + \'_("o" + "s").popen("cat {p}").read())\')',
        "simplify(Symbol.mro)",
        "(40).to_bytes(1, big.name).decode()",
        "simplify(x.name + y.name)",
        "sqrt.func_globals",
        'simplify(f"Symbol({1})")',
        'simplify(b"x".decode())',
        "simplify(\uff02Symbol(1)\uff02)",
        "\uff4fpen(1)",
        "simplify(`x`)",
        "x . name",
        # Attributzugriff an der Pruefung vorbei: Rueckstrich mit Zeilenumbruch, Kommentar
        "simplify(x.\\\nname + y.\\\nname)",
        "(x.#c\nname)",
        "(x.\n name)",
        "simplify(x.\tname)",
        "1 + 2 # Kommentar",
    ]
    for expr in attacks:
        out = _run(expr)
        assert out.startswith("Berechnungsfehler: "), (expr, out)
        assert secret_content not in out, (expr, out)


@pytest.mark.parametrize("expr, expected", [
    ("17*23", "Ergebnis: 391"),
    ("2+2", "Ergebnis: 4"),
    ("sqrt(2)", "Ergebnis: 1.414213562"),
    ("1.5*3", "Ergebnis: 4.5"),
    ("3.2e-3*2", "Ergebnis: 0.0064"),
    ("2x + 3x", "Ergebnis: 5.0*x"),
    ("5!", "Ergebnis: 120"),
    ("factorial(5)", "Ergebnis: 120"),
    ("Rational(1,3)+Rational(1,6)", "Ergebnis: 0.5"),
    ("solve(x**2-4, x)", "Loesung: [-2, 2]"),
    ("diff(sin(x)*x, x)", "Ableitung: x*cos(x) + sin(x)"),
    ("integrate(x**2, x)", "Integral: 0.333333333333333*x**3"),
    ("simplify((x**2-1)/(x-1))", "Vereinfacht: x + 1.0"),
    ("limit(sin(x)/x, x, 0)", "Ergebnis: 1"),
    ("Sum(k, (k, 1, 10))", "Ergebnis: 55"),
    ("Matrix([[1,2],[3,4]])**2", "Ergebnis: Matrix([[7.00000000000000, 10.0000000000000], [15.0000000000000, 22.0000000000000]])"),
    ("(1234.56 * 0.19) + 1234.56", "Ergebnis: 1469.1264"),
    (".5 + .25", "Ergebnis: 0.75"),
    ("2**10", "Ergebnis: 1024"),
    ("exp(1)", "Ergebnis: 2.718281828"),
    ("log(100, 10)", "Ergebnis: 2"),
    ("gcd(12, 18)", "Ergebnis: 6"),
    ("Abs(-3)", "Ergebnis: 3"),
    ("2.5 * sqrt(144) + pi", "Ergebnis: 33.14159265"),
])
def test_calculations_unchanged(expr, expected):
    assert _run(expr) == expected


def test_exec_code_files_in_data_and_results_in_out():
    sample_content = b"Spalte1,Spalte2\nWertA,100\nWertB,200\n"
    b64 = base64.b64encode(sample_content).decode("ascii")
    files = [{"name": "input.csv", "content": b64}]

    code = (
        "with open('./data/input.csv') as f:\n"
        "    lines = f.readlines()\n"
        "print(f'Gelesen: {len(lines)} Zeilen')\n"
        "with open('./out/summary.txt', 'w') as f:\n"
        "    f.write('Auswertung erfolgreich.')\n"
    )

    res = _exec_code(code, "python", timeout=10.0, files=files)
    assert "error" not in res
    assert "Gelesen: 3 Zeilen" in res.get("result", "")
    out_files = res.get("files", [])
    assert len(out_files) == 1
    assert out_files[0]["name"] == "summary.txt"
    decoded = base64.b64decode(out_files[0]["content"]).decode("utf-8")
    assert decoded == "Auswertung erfolgreich."


def test_exec_code_ignores_symlinks_and_subdirectories_in_out():
    code = (
        "with open('./out/real.txt', 'w') as f:\n"
        "    f.write('real data')\n"
        "import os\n"
        "os.makedirs('./out/subdir', exist_ok=True)\n"
        "with open('./out/subdir/ignored.txt', 'w') as f:\n"
        "    f.write('in subdir')\n"
        "try:\n"
        "    os.symlink('./out/real.txt', './out/symlink.txt')\n"
        "except OSError:\n"
        "    pass\n"
        "print('Done')\n"
    )
    res = _exec_code(code, "python", timeout=10.0)
    assert "error" not in res
    out_files = res.get("files", [])
    names = [f["name"] for f in out_files]
    assert "real.txt" in names
    assert "subdir" not in names
    assert "ignored.txt" not in names
    assert "symlink.txt" not in names


def test_exec_code_limits_max_files_and_adds_hint():
    code = (
        "for i in range(13):\n"
        "    with open(f'./out/file_{i:02d}.txt', 'w') as f:\n"
        "        f.write(f'Content {i}')\n"
        "print('Generated')\n"
    )
    res = _exec_code(code, "python", timeout=10.0)
    assert "error" not in res
    out_files = res.get("files", [])
    assert len(out_files) == 10
    assert "weggelassen" in res.get("result", "")


def test_exec_code_cleans_up_temp_run_dir(monkeypatch):
    created_dirs = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    res = _exec_code("print('cleanup check')", "python", timeout=10.0)
    assert "error" not in res
    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])


def test_exec_code_rejects_powershell():
    res = _exec_code("Write-Output 'pwsh'", "powershell", timeout=5.0)
    assert "error" in res
    assert "Nicht unterstuetzte Sprache" in res["error"]


@nur_im_container
def test_two_consecutive_runs_isolate_files_and_tmp():
    # Lauf 1 legt Datei in /tmp ab und hat eine Eingabedatei in ./data/
    b64_1 = base64.b64encode(b"Run 1 Data").decode("ascii")
    files_1 = [{"name": "run1_input.txt", "content": b64_1}]
    code_1 = (
        "import os\n"
        "with open('/tmp/leak_test.txt', 'w') as f:\n"
        "    f.write('geheim')\n"
        "with open('./data/run1_input.txt') as f:\n"
        "    txt = f.read()\n"
        "print('Lauf 1 fertig: ' + txt)\n"
    )
    res_1 = _exec_code(code_1, "python", timeout=10.0, files=files_1)
    assert "error" not in res_1
    assert "Lauf 1 fertig: Run 1 Data" in res_1.get("result", "")

    # Lauf 2 prueft, dass weder /tmp/leak_test.txt noch ./data/run1_input.txt existieren
    code_2 = (
        "import os\n"
        "leak_exists = os.path.exists('/tmp/leak_test.txt')\n"
        "data_exists = os.path.exists('./data/run1_input.txt')\n"
        "print(f'LEAK:{leak_exists},DATA:{data_exists}')\n"
    )
    res_2 = _exec_code(code_2, "python", timeout=10.0, files=[])
    assert "error" not in res_2
    assert "LEAK:False,DATA:False" in res_2.get("result", "")


@nur_im_container
def test_sysv_ipc_is_disabled_but_multiprocessing_works():
    """SysV-Objekte (shmget, msgget, semget) ueberlebten das Aufraeumen zwischen zwei
    Laeufen und gehoerten alle dem Benutzer runner. Die sysctls aus compose schalten sie
    ab; multiprocessing nutzt POSIX-Semaphoren und laeuft weiter."""
    code = (
        "import ctypes, ctypes.util, multiprocessing as mp\n"
        "libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)\n"
        "shm = libc.shmget(0, ctypes.c_size_t(4096), 0o1600)\n"
        "msg = libc.msgget(0, 0o1600)\n"
        "sem = libc.semget(0, 1, 0o1600)\n"
        "q = mp.Queue(); q.put(7)\n"
        "print(f'SHM:{shm},MSG:{msg},SEM:{sem},MP:{q.get(timeout=5)}')\n"
    )
    res = _exec_code(code, "python", timeout=10.0, files=[])
    assert "error" not in res, res
    assert "SHM:-1,MSG:-1,SEM:-1,MP:7" in res.get("result", "")


@nur_im_container
def test_forked_background_process_is_killed():
    if not hasattr(os, "fork") or not os.path.exists("/proc"):
        pytest.skip("Test erfordert os.fork und /proc (Linux-Sandbox)")

    # Lauf 1 startet per fork+setsid einen Hintergrundprozess, der 30s schlafen wuerde.
    # Kindprozess leitet stdin/stdout/stderr um, damit die Pipes des Elternprozesses
    # schliessen koennen und communicate() nicht auf das Kind wartet.
    code = (
        "import os, time, sys\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    devnull = os.open(os.devnull, os.O_RDWR)\n"
        "    os.dup2(devnull, 0)\n"
        "    os.dup2(devnull, 1)\n"
        "    os.dup2(devnull, 2)\n"
        "    time.sleep(30)\n"
        "    sys.exit(0)\n"
        "else:\n"
        "    print(f'FORKED:{pid}', flush=True)\n"
    )
    res = _exec_code(code, "python", timeout=10.0)
    assert "error" not in res
    out = res.get("result", "")
    assert "FORKED:" in out
    forked_pid = int(out.split("FORKED:")[1].strip().split()[0])

    # Nach dem Lauf muss der forked_pid durch _cleanup_sandbox via SIGKILL beendet worden sein.
    # Ein per SIGKILL beendeter verwaister Prozess ist entweder bereits aus /proc entfernt
    # oder im Zombie-Zustand ('Z (zombie)'), falls PID 1 ihn noch nicht geerntet hat.
    import time
    is_running = True
    for _ in range(20):
        try:
            with open(f"/proc/{forked_pid}/status") as f:
                content = f.read().lower()
                if "state:\tz (zombie)" in content or "state: z (zombie)" in content:
                    is_running = False
                    break
        except (FileNotFoundError, ProcessLookupError, OSError):
            is_running = False
            break
        time.sleep(0.05)
    assert not is_running, f"Hintergrundprozess PID {forked_pid} laeuft noch!"


def test_input_files_max_count_exceeded():
    files = [{"name": f"f_{i}.txt", "content": base64.b64encode(b"x").decode("ascii")} for i in range(21)]
    res = _exec_code("print(1)", "python", timeout=5.0, files=files)
    assert "error" in res
    assert "Zu viele Eingabedateien" in res["error"]


def test_input_files_max_bytes_exceeded():
    big_bytes = b"0" * (65 * 1024 * 1024)
    b64_big = base64.b64encode(big_bytes).decode("ascii")
    files = [{"name": "big.bin", "content": b64_big}]
    res = _exec_code("print(1)", "python", timeout=5.0, files=files)
    assert "error" in res
    assert "Eingabedateien zu gross" in res["error"]


def test_cleanup_does_nothing_outside_the_container(monkeypatch):
    #--- Ausserhalb des Abbilds darf das Aufraeumen weder Prozesse beenden noch /tmp leeren.
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    monkeypatch.delenv("ARGUS_SANDBOX_CONTAINER", raising=False)
    aufrufe = []
    monkeypatch.setattr(mod, "_kill_other_processes", lambda **kw: aufrufe.append("kill"))
    monkeypatch.setattr(mod, "_clean_tmp_dir", lambda: aufrufe.append("tmp"))
    mod._cleanup_sandbox()
    assert aufrufe == []
    monkeypatch.setenv("ARGUS_SANDBOX_CONTAINER", "1")
    mod._cleanup_sandbox()
    assert aufrufe == ["kill", "tmp"]


def test_sandbox_busy_lock_timeout(monkeypatch):
    class FakeBusyLock:
        def acquire(self, timeout=None):
            return False
        def release(self):
            pass

    import sys
    target_mod = "calc_sandbox.app" if "calc_sandbox.app" in sys.modules else "app"
    monkeypatch.setattr(f"{target_mod}._EXEC_LOCK", FakeBusyLock())
    res = _exec_code("print(1)", "python", timeout=5.0)
    assert res == {"error": "Sandbox belegt, bitte erneut versuchen."}


@nur_im_container
def test_code_runs_under_different_uid_and_without_capabilities():
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)

    service_uid = os.getuid()
    service_gid = os.getgid()
    service_pid = os.getpid()

    code = (
        "import json, os\n"
        "caps = {}\n"
        "with open('/proc/self/status') as f:\n"
        "    for line in f:\n"
        "        if line.startswith(('CapInh:', 'CapPrm:', 'CapEff:', 'CapAmb:')):\n"
        "            k, v = line.split(':', 1)\n"
        "            caps[k.strip()] = int(v.strip(), 16)\n"
        f"proc_Inspect_blocked = True\n"
        f"for name in ('environ', 'maps', 'mem', 'fd'):\n"
        f"    try:\n"
        f"        p = f'/proc/{service_pid}/{{name}}'\n"
        f"        os.listdir(p) if name == 'fd' else open(p, 'rb').read()\n"
        f"        proc_Inspect_blocked = False\n"
        f"    except PermissionError:\n"
        f"        pass\n"
        "info = {\n"
        "    'uid': os.getuid(),\n"
        "    'euid': os.geteuid(),\n"
        "    'gid': os.getgid(),\n"
        "    'egid': os.getegid(),\n"
        "    'groups': os.getgroups(),\n"
        "    'caps': caps,\n"
        "    'proc_blocked': proc_Inspect_blocked,\n"
        "}\n"
        "print(json.dumps(info))\n"
    )
    resp = client.post("/execute", json={"code": code, "timeout": 10.0})
    assert resp.status_code == 200
    data = resp.json()
    assert "error" not in data, data
    import json
    info = json.loads(data["result"].strip())
    assert info["uid"] != service_uid
    assert info["euid"] != service_uid
    assert info["gid"] != service_gid
    assert info["egid"] != service_gid
    assert info["uid"] == getattr(mod, "_RUNNER_UID", 10001)
    assert info["groups"] == []
    assert info["caps"] == {"CapInh": 0, "CapPrm": 0, "CapEff": 0, "CapAmb": 0}
    assert info["proc_blocked"] is True


@nur_im_container
def test_signal_to_service_process_is_rejected_and_service_continues():
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    service_pid = os.getpid()

    code = (
        "import os, signal\n"
        "ergebnisse = []\n"
        f"for pid in (1, {service_pid}):\n"
        "    for sig in (0, signal.SIGTERM, signal.SIGKILL):\n"
        "        try:\n"
        "            os.kill(pid, sig)\n"
        "            ergebnisse.append(f'ERLAUBT:{pid}:{sig}')\n"
        "        except PermissionError:\n"
        "            ergebnisse.append(f'BLOCKIERT:{pid}:{sig}')\n"
        "print(','.join(ergebnisse))\n"
    )
    resp = client.post("/execute", json={"code": code, "timeout": 10.0})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, body
    out = body.get("result", "").strip()
    assert "ERLAUBT:" not in out
    assert f"BLOCKIERT:{service_pid}:0" in out
    assert f"BLOCKIERT:{service_pid}:{int(os.sys.modules['signal'].SIGKILL)}" in out

    # Dienst antwortet danach weiter auf allen Endpunkten
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/calculate", json={"expression": "21 * 2"}).json() == {"result": "Ergebnis: 42"}
    followup = client.post("/execute", json={"code": "print('weiter_aktiv')"}).json()
    assert followup.get("result", "").strip() == "weiter_aktiv"


def _find_non_service_pids(service_pid: int) -> list[int]:
    """Hilfsfunktion fuer Tests: sucht aktive Prozesse in /proc ausser PID 1 und Dienstprozess."""
    keep = {1, service_pid}
    try:
        ppid = os.getppid()
        if ppid > 0:
            keep.add(ppid)
    except Exception:
        pass
    active = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in keep:
            continue
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            continue
        if "State:\tZ" in txt or "State:\tX" in txt:
            continue
        active.append(pid)
    return active


@nur_im_container
def test_background_session_process_gone_before_next_run(monkeypatch):
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    service_pid = os.getpid()

    # Lauf 1 startet mehrere Hintergrundprozesse in eigener Sitzung, die sich
    # gegenseitig am Leben halten wollen, und gibt deren PIDs aus.
    code_1 = (
        "import os, time, sys\n"
        "pids = []\n"
        "for _ in range(3):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os.setsid()\n"
        "        devnull = os.open(os.devnull, os.O_RDWR)\n"
        "        os.dup2(devnull, 0)\n"
        "        os.dup2(devnull, 1)\n"
        "        os.dup2(devnull, 2)\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    pids.append(pid)\n"
        "print('PIDS:' + ','.join(str(p) for p in pids), flush=True)\n"
    )
    res_1 = _exec_code(code_1, "python", timeout=10.0)
    assert "error" not in res_1, res_1
    pid_line = [l for l in res_1["result"].splitlines() if l.startswith("PIDS:")][0]
    spawned_pids = [int(x) for x in pid_line.split(":", 1)[1].split(",")]
    assert len(spawned_pids) == 3

    # Vor dem naechsten Lauf darf in /proc kein einziger Prozess des Lauf-Benutzers mehr existieren
    assert _find_non_service_pids(service_pid) == []

    # Lauf 2 prueft aus Sicht des Lauf-Benutzers, dass keiner der Prozesse aus Lauf 1 mehr existiert
    code_2 = (
        "import os\n"
        f"noch_da = [p for p in {spawned_pids!r} if os.path.exists(f'/proc/{{p}}')]\n"
        "print(f'NOCH_DA:{len(noch_da)}')\n"
    )
    res_2 = _exec_code(code_2, "python", timeout=10.0)
    assert "error" not in res_2, res_2
    assert "NOCH_DA:0" in res_2["result"]

    # Fail-Closed-Pruefung: Wenn sich nicht bestaetigen laesst, dass alle Prozesse
    # des Lauf-Benutzers beendet sind, verweigert die Sandbox den naechsten Lauf.
    if hasattr(mod, "_KILL_MAX_ATTEMPTS"):
        monkeypatch.setattr(mod, "_KILL_MAX_ATTEMPTS", 2)
    if hasattr(mod, "_list_runner_processes"):
        monkeypatch.setattr(mod, "_list_runner_processes", lambda *a, **kw: [99999])
    res_blocked = _exec_code("print('darf_nicht_laufen')", "python", timeout=5.0)
    assert "error" in res_blocked
    assert "Sandbox-Aufraeumen fehlgeschlagen" in res_blocked["error"]


@nur_im_container
def test_tmp_and_data_isolated_across_runs_even_with_locked_dirs():
    # Lauf 1 legt Datei in /tmp sowie in einem gesperrten Ordner (/tmp/locked_dir mit Mode 0000)
    # ab und merkt sich den absoluten Pfad seiner Eingabedatei aus data/.
    secret_1 = b"Vertrauliche_Konto1_Daten_98765"
    files_1 = [{"name": "konto1.txt", "content": base64.b64encode(secret_1).decode("ascii")}]
    code_1 = (
        "import os\n"
        "with open('/tmp/offen.txt', 'w') as f:\n"
        "    f.write('geheim_offen')\n"
        "os.makedirs('/tmp/gesperrt/tief', exist_ok=True)\n"
        "with open('/tmp/gesperrt/tief/geheim.txt', 'w') as f:\n"
        "    f.write('geheim_tief')\n"
        "os.chmod('/tmp/gesperrt/tief', 0o000)\n"
        "os.chmod('/tmp/gesperrt', 0o000)\n"
        "data_abs = os.path.abspath('./data/konto1.txt')\n"
        "assert open(data_abs, 'rb').read() == b'Vertrauliche_Konto1_Daten_98765'\n"
        "print('PFAD:' + data_abs)\n"
    )
    res_1 = _exec_code(code_1, "python", timeout=10.0, files=files_1)
    assert "error" not in res_1, res_1
    run1_data_path = [l.split("PFAD:", 1)[1].strip() for l in res_1["result"].splitlines() if l.startswith("PFAD:")][0]

    # Lauf 2 darf weder /tmp/offen.txt noch /tmp/gesperrt noch die data/-Datei aus Lauf 1 sehen
    code_2 = (
        "import os\n"
        "try:\n"
        "    os.chmod('/tmp/gesperrt', 0o700)\n"
        "    os.chmod('/tmp/gesperrt/tief', 0o700)\n"
        "except OSError:\n"
        "    pass\n"
        "checks = {\n"
        "    'offen': os.path.exists('/tmp/offen.txt'),\n"
        "    'gesperrt': os.path.exists('/tmp/gesperrt'),\n"
        "    'rel_data': os.path.exists('./data/konto1.txt'),\n"
        f"    'abs_data': os.path.exists({run1_data_path!r}),\n"
        "    'tmp_items': os.listdir('/tmp'),\n"
        "}\n"
        "import json\n"
        "print(json.dumps(checks))\n"
    )
    res_2 = _exec_code(code_2, "python", timeout=10.0, files=[])
    assert "error" not in res_2, res_2
    import json
    checks = json.loads(res_2["result"].strip())
    assert checks["offen"] is False
    assert checks["gesperrt"] is False
    assert checks["rel_data"] is False
    assert checks["abs_data"] is False
    assert "offen.txt" not in checks["tmp_items"]
    assert "gesperrt" not in checks["tmp_items"]


@nur_im_container
def test_calculate_timeout_kills_process_and_leaves_no_runner_in_proc():
    import threading
    import time
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    service_pid = os.getpid()
    calc_uid = getattr(mod, "_CALC_UID", 10002)

    response_holder = {}

    def _call_calc():
        response_holder["resp"] = client.post(
            "/calculate",
            json={"expression": "factor(x**10000 - 1)"},
        )

    t = threading.Thread(target=_call_calc)
    t.start()

    # Waehrend die Rechnung laeuft, muss ein Rechenprozess unter uid 10002 (calc) in /proc sichtbar sein
    observed_calc_pids = []
    for _ in range(40):
        pids = mod._list_runner_processes({1, service_pid}, target_uid=calc_uid)
        if pids:
            observed_calc_pids = pids
            break
        time.sleep(0.05)

    assert len(observed_calc_pids) >= 1, "Rechenprozess unter calc-UID (10002) wurde in /proc nicht beobachtet"

    t.join(timeout=15.0)
    assert not t.is_alive(), "/calculate-Aufruf kehrte nicht rechtzeitig zurueck"
    resp = response_holder["resp"]
    assert resp.status_code == 200
    result_text = resp.json().get("result", "")
    assert "Berechnungsfehler: Timeout nach 5 Sekunden" in result_text

    # Unmittelbar nach der Antwort darf kein Rechenprozess der uid 10002 mehr in /proc existieren
    for pid in observed_calc_pids:
        assert not os.path.exists(f"/proc/{pid}"), f"Rechenprozess {pid} existiert nach Timeout noch in /proc"
    assert mod._list_runner_processes({1, service_pid}, target_uid=calc_uid) == []


@nur_im_container
def test_output_files_with_restrictive_permissions_0600_arrive_in_response():
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    service_uid = os.getuid()

    code = (
        "import os\n"
        "fd1 = os.open('./out/eng_0600.txt', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "os.write(fd1, b'Geheimer Bericht 0600')\n"
        "os.close(fd1)\n"
        "os.chmod('./out/eng_0600.txt', 0o600)\n"
        "fd2 = os.open('./out/eng_0400.txt', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)\n"
        "os.write(fd2, b'Nur-Lesen-Eigentuemer 0400')\n"
        "os.close(fd2)\n"
        "os.chmod('./out/eng_0400.txt', 0o400)\n"
        "print(f'UID:{os.getuid()}')\n"
    )
    resp = client.post("/execute", json={"code": code, "timeout": 10.0})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, body
    child_uid = int(body.get("result", "").strip().split("UID:")[1].split()[0])
    assert child_uid != service_uid
    files = {f["name"]: f for f in body.get("files", [])}
    assert "eng_0600.txt" in files
    assert "eng_0400.txt" in files
    assert base64.b64decode(files["eng_0600.txt"]["content"]) == b"Geheimer Bericht 0600"
    assert files["eng_0600.txt"]["size"] == len(b"Geheimer Bericht 0600")
    assert base64.b64decode(files["eng_0400.txt"]["content"]) == b"Nur-Lesen-Eigentuemer 0400"
    assert files["eng_0400.txt"]["size"] == len(b"Nur-Lesen-Eigentuemer 0400")


@nur_im_container
def test_calculate_does_not_wait_behind_running_execute():
    import threading
    import time
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    service_pid = os.getpid()

    exec_holder = {}

    def _run_slow_exec():
        exec_holder["resp"] = client.post(
            "/execute",
            json={"code": "import time\ntime.sleep(6)\nprint('exec_fertig_6s')\n", "timeout": 15.0},
        )

    exec_thread = threading.Thread(target=_run_slow_exec)
    exec_thread.start()

    # Warte kurz, bis der /execute-Prozess (uid 10001) tatsaechlich laeuft
    runner_started = False
    for _ in range(40):
        if mod._list_runner_processes({1, service_pid}, target_uid=mod._RUNNER_UID):
            runner_started = True
            break
        time.sleep(0.05)
    assert runner_started, "/execute-Prozess unter runner-UID (10001) ist nicht gestartet"

    # Waehrend /execute noch ~5.5s schlaeft, muss /calculate in unter 2s antworten
    t0 = time.monotonic()
    calc_resp = client.post("/calculate", json={"expression": "6 * 7"})
    calc_elapsed = time.monotonic() - t0
    assert calc_resp.status_code == 200
    assert calc_resp.json() == {"result": "Ergebnis: 42"}
    assert calc_elapsed < 2.0, f"/calculate hat {calc_elapsed:.2f}s gewartet (Limit: < 2.0s)"

    # Das laufende /execute darf durch /calculate nicht abgebrochen worden sein und liefert normal
    exec_thread.join(timeout=12.0)
    assert not exec_thread.is_alive(), "/execute-Thread kehrte nicht rechtzeitig zurueck"
    exec_resp = exec_holder["resp"]
    assert exec_resp.status_code == 200
    exec_body = exec_resp.json()
    assert "error" not in exec_body, exec_body
    assert "exec_fertig_6s" in exec_body.get("result", "")


def test_no_silent_fallback_when_privilege_drop_unavailable(monkeypatch):
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    expected_msg = "Sandbox falsch gestartet: Dienst muss als root mit SETUID/SETGID/KILL laufen"

    # Im Container (ARGUS_SANDBOX_CONTAINER=1) ohne Moeglichkeit zum Benutzerwechsel
    # (z. B. Dienst laeuft nicht als root): /execute und /calculate verweigern.
    monkeypatch.setenv("ARGUS_SANDBOX_CONTAINER", "1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    exec_resp = client.post("/execute", json={"code": "print('darf_nicht')", "timeout": 5.0})
    assert exec_resp.status_code == 200
    assert expected_msg in exec_resp.json().get("error", "")

    calc_resp = client.post("/calculate", json={"expression": "6 * 7"})
    assert calc_resp.status_code == 200
    assert expected_msg in calc_resp.json().get("result", "")

    # Ausserhalb des Containers (ARGUS_SANDBOX_CONTAINER nicht gesetzt) bleibt das Verhalten wie bisher.
    monkeypatch.delenv("ARGUS_SANDBOX_CONTAINER", raising=False)
    calc_outside = client.post("/calculate", json={"expression": "6 * 7"})
    assert calc_outside.status_code == 200
    assert calc_outside.json() == {"result": "Ergebnis: 42"}

    exec_outside = client.post("/execute", json={"code": "print(6 * 7)", "timeout": 5.0})
    assert exec_outside.status_code == 200
    assert exec_outside.json().get("result", "").strip() == "42"



def test_requests_without_sandbox_token_are_rejected(monkeypatch):
    """Mit SANDBOX_TOKEN nimmt die Sandbox nur Anfragen mit diesem Token an. So kann
    Code in einer Replik keine andere Replik (und nicht den eigenen Dienst) aufrufen."""
    from fastapi.testclient import TestClient
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)
    monkeypatch.setattr(mod, "_SANDBOX_TOKEN", "richtiges-token-0123456789")

    assert client.get("/healthz").status_code == 200
    for headers in ({}, {"X-Sandbox-Token": "falsch"}, {"X-Sandbox-Token": "richtiges-token-012345678"}):
        r_exec = client.post("/execute", json={"code": "print(1)"}, headers=headers)
        assert r_exec.status_code == 401
        r_calc = client.post("/calculate", json={"expression": "1+1"}, headers=headers)
        assert r_calc.status_code == 401

    ok = client.post("/calculate", json={"expression": "6 * 7"},
                     headers={"X-Sandbox-Token": "richtiges-token-0123456789"})
    assert ok.status_code == 200
    assert ok.json() == {"result": "Ergebnis: 42"}


def test_sandbox_token_is_removed_from_environment():
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    assert "SANDBOX_TOKEN" not in os.environ
    assert isinstance(mod._SANDBOX_TOKEN, str)


def _warte_auf_zombie(pid: int, timeout: float = 3.0) -> None:
    import time
    ende = time.monotonic() + timeout
    while time.monotonic() < ende:
        try:
            with open(f"/proc/{pid}/stat", "rb") as fp:
                if fp.read().rsplit(b")", 1)[-1].split()[0] == b"Z":
                    return
        except OSError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"Prozess {pid} ist nicht als beendet sichtbar geworden")


def _fork_und_ende(code: int) -> int:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        os._exit(code)
    return pid


@nur_im_container
def test_reap_zombies_leaves_tracked_children_and_reaps_the_rest():
    #--- /execute und /calculate laufen nebeneinander. Erntet der eine Pfad das Kind,
    #--- auf das der andere wartet, geht dessen Rueckgabewert verloren und ein
    #--- fehlgeschlagener Lauf sieht aus wie ein gelungener.
    import sys
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]

    eingetragen = _fork_und_ende(7)
    with mod._CHILD_LOCK:
        mod._TRACKED_CHILD_PIDS.add(eingetragen)
    verwaist = _fork_und_ende(3)
    try:
        _warte_auf_zombie(eingetragen)
        _warte_auf_zombie(verwaist)

        mod._reap_zombies()

        # Das eingetragene Kind ist noch da, sein Rueckgabewert ist abholbar.
        wpid, status = os.waitpid(eingetragen, 0)
        assert wpid == eingetragen
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 7
        # Das nicht eingetragene wurde geerntet.
        with pytest.raises(ChildProcessError):
            os.waitpid(verwaist, 0)
    finally:
        mod._untrack_child(eingetragen)
    assert eingetragen not in mod._TRACKED_CHILD_PIDS


@nur_im_container
def test_failed_run_stays_failed_while_calculations_run_in_parallel():
    from fastapi.testclient import TestClient
    import sys
    import threading
    mod = sys.modules.get("calc_sandbox.app") or sys.modules["app"]
    client = TestClient(mod.app)

    stop = threading.Event()
    rechnungen = {"n": 0, "falsch": 0}

    def _rechnen():
        while not stop.is_set():
            r = client.post("/calculate", json={"expression": "20 + 22"})
            rechnungen["n"] += 1
            if r.json() != {"result": "Ergebnis: 42"}:
                rechnungen["falsch"] += 1

    t = threading.Thread(target=_rechnen, daemon=True)
    t.start()
    try:
        for i in range(5):
            resp = client.post("/execute", json={
                "code": f"import sys\nprint('lauf_{i}')\nsys.exit(3)", "timeout": 10.0})
            body = resp.json()
            assert "error" in body and "result" not in body, body
            assert f"lauf_{i}" in body["error"]
    finally:
        stop.set()
        t.join(timeout=20.0)
    assert not t.is_alive()
    assert rechnungen["n"] > 0 and rechnungen["falsch"] == 0, rechnungen


#--- Paritaetstest fuer die Dateinamensbereinigung zwischen Sandbox und rag_backend
PARITY_TEST_CASES_VALID = [
    ("Prüfbericht_Übergabe.pdf", "Prüfbericht_Übergabe.pdf"),
    ("Pru\u0308fbericht_U\u0308bergabe.pdf", "Prüfbericht_Übergabe.pdf"),  # NFD -> NFC
    ("Bericht (2026) - Entwurf_v1.0.txt", "Bericht (2026) - Entwurf_v1.0.txt"),
    ("отчет_2026.xlsx", "отчет_2026.xlsx"),
    ("日本語_メモ.txt", "日本語_メモ.txt"),
    ("bericht:mit*fragezeichen?.txt", "bericht_mit_fragezeichen_.txt"),
    ("datei.pdf. ", "datei.pdf"),
    ("notiz.txt...", "notiz.txt"),
    ("...geheim.txt", "geheim.txt"),
    ("/pfad/zu/dokument.pdf", "dokument.pdf"),
    ("..\\windows\\notiz.txt", "notiz.txt"),
]

PARITY_TEST_CASES_INVALID = [
    "",
    "   ",
    ".",
    "..",
    "...",
    "CON",
    "con.txt",
    "PRN.pdf",
    "aux.dat",
    "NUL",
    "COM1.txt",
    "com9.png",
    "lpt1.csv",
    "LPT8.tar.gz",
]


@pytest.mark.parametrize("raw, expected", PARITY_TEST_CASES_VALID)
def test_sanitize_filename_parity_valid(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", PARITY_TEST_CASES_INVALID)
def test_sanitize_filename_parity_invalid(raw):
    with pytest.raises(ValueError):
        sanitize_filename(raw)


def test_sanitize_filename_parity_length_limits():
    long_ascii = "a" * 200 + ".txt"
    cleaned = sanitize_filename(long_ascii)
    assert len(cleaned) == 180
    assert len(cleaned.encode("utf-8")) <= 240

    long_umlaut = "ä" * 150 + ".txt"
    cleaned_umlaut = sanitize_filename(long_umlaut)
    assert len(cleaned_umlaut) <= 180
    assert len(cleaned_umlaut.encode("utf-8")) <= 240



# ---- Ein Code-Lauf je Container (SANDBOX_ONE_RUN)

def _sandbox_mod():
    import sys
    return sys.modules.get("calc_sandbox.app") or sys.modules["app"]


def _app_recording_response(mod, events):
    """ASGI-Huelle um die Sandbox-App: vermerkt, wann die Antwort vollstaendig
    verschickt ist, damit die Tests die Reihenfolge zu _retire pruefen koennen."""
    async def wrapped(scope, receive, send):
        async def send_and_record(message):
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                events.append("response")
        await mod.app(scope, receive, send_and_record)
    return wrapped


@pytest.fixture
def one_run(monkeypatch):
    """SANDBOX_ONE_RUN an, frischer Anspruchszustand, _retire und die Wartefunktion
    ersetzt (es wird nichts beendet und nicht gewartet)."""
    mod = _sandbox_mod()
    monkeypatch.setenv("SANDBOX_ONE_RUN", "true")
    monkeypatch.setattr(mod, "_CLAIMED", False)
    monkeypatch.setattr(mod, "_RETIRED", False)
    monkeypatch.setattr(mod, "_FALLBACK_TIMER", None)
    events: list = []

    def fake_retire():
        events.append("retire")

    async def fake_wait(seconds):
        events.append(("wait", seconds))

    monkeypatch.setattr(mod, "_retire", fake_retire)
    monkeypatch.setattr(mod, "_retire_wait", fake_wait)
    yield mod, events
    timer = mod._FALLBACK_TIMER
    if timer is not None:
        timer.cancel()


def test_one_run_first_run_ok_second_503_retire_once_after_response(one_run, monkeypatch):
    from fastapi.testclient import TestClient
    import time as _time
    mod, events = one_run

    exec_calls = []
    real_exec = mod._exec_code

    def counting_exec(*args, **kwargs):
        exec_calls.append(args[0])
        return real_exec(*args, **kwargs)

    monkeypatch.setattr(mod, "_exec_code", counting_exec)
    #--- Container gerade erst gestartet (vor 3 s): bis 10 s muss gewartet werden.
    monkeypatch.setattr(mod, "_SERVICE_STARTED_AT", _time.monotonic() - 3.0)
    client = TestClient(_app_recording_response(mod, events))

    r1 = client.post("/execute", json={"code": "print(6 * 7)", "language": "python", "timeout": 5.0})
    assert r1.status_code == 200
    assert r1.json()["result"].strip() == "42"
    assert exec_calls == ["print(6 * 7)"]

    #--- Erst Antwort, dann Wartezeit bis zur Mindestlaufzeit, dann genau ein _retire.
    assert [e if isinstance(e, str) else e[0] for e in events] == ["response", "wait", "retire"]
    #--- Gewartet wird der Rest bis 10 s seit dem Start: hoechstens 7 s, abzueglich
    #--- der Dauer des Laufs selbst (pandas-Import, je nach Rechner 0,5 bis 2 s).
    waited = events[1][1]
    assert 0.0 < waited <= 7.0 + 0.5

    r2 = client.post("/execute", json={"code": "print('zweiter Lauf')", "language": "python", "timeout": 5.0})
    assert r2.status_code == 503
    assert r2.json() == {"error": "Sandbox wird erneuert."}
    assert exec_calls == ["print(6 * 7)"]
    assert events.count("retire") == 1


def test_one_run_no_wait_when_container_ran_long_enough(one_run, monkeypatch):
    from fastapi.testclient import TestClient
    import time as _time
    mod, events = one_run
    monkeypatch.setattr(mod, "_exec_code", lambda *a, **kw: {"result": "ok\n", "files": []})
    monkeypatch.setattr(mod, "_SERVICE_STARTED_AT", _time.monotonic() - 60.0)
    client = TestClient(_app_recording_response(mod, events))

    r = client.post("/execute", json={"code": "print('ok')"})
    assert r.status_code == 200
    assert events == ["response", "retire"]


def test_one_run_failed_run_also_retires(one_run, monkeypatch):
    from fastapi.testclient import TestClient
    import time as _time
    mod, events = one_run
    monkeypatch.setattr(mod, "_SERVICE_STARTED_AT", _time.monotonic() - 60.0)

    def boom(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(mod, "_exec_code", boom)
    client = TestClient(_app_recording_response(mod, events))

    r = client.post("/execute", json={"code": "print(1)"})
    assert r.status_code == 200
    assert "kaputt" in r.json()["error"]
    assert events == ["response", "retire"]
    r2 = client.post("/execute", json={"code": "print(1)"})
    assert r2.status_code == 503
    assert events.count("retire") == 1


def test_one_run_concurrent_execute_exactly_one_runs(one_run, monkeypatch):
    from fastapi.testclient import TestClient
    import threading as _threading
    import time as _time
    mod, events = one_run
    monkeypatch.setattr(mod, "_SERVICE_STARTED_AT", _time.monotonic() - 60.0)

    release = _threading.Event()
    exec_calls = []

    def slow_exec(*args, **kwargs):
        exec_calls.append(args[0])
        release.wait(timeout=20)
        return {"result": "fertig\n", "files": []}

    monkeypatch.setattr(mod, "_exec_code", slow_exec)

    results = []
    results_lock = _threading.Lock()

    def worker(i):
        client = TestClient(mod.app)
        r = client.post("/execute", json={"code": f"print({i})"})
        with results_lock:
            results.append((r.status_code, r.json()))

    n = 6
    threads = [_threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    #--- Alle ausser dem einen beanspruchenden Lauf kommen sofort mit 503 zurueck.
    deadline = _time.monotonic() + 20
    while _time.monotonic() < deadline:
        with results_lock:
            if len(results) >= n - 1:
                break
        _time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == n
    ok = [body for code, body in results if code == 200]
    rejected = [body for code, body in results if code == 503]
    assert len(ok) == 1 and ok[0]["result"] == "fertig\n"
    assert len(rejected) == n - 1
    assert all(body == {"error": "Sandbox wird erneuert."} for body in rejected)
    assert len(exec_calls) == 1
    assert events.count("retire") == 1


def test_one_run_calculate_does_not_claim(one_run, monkeypatch):
    from fastapi.testclient import TestClient
    import time as _time
    mod, events = one_run
    monkeypatch.setattr(mod, "_SERVICE_STARTED_AT", _time.monotonic() - 60.0)
    monkeypatch.setattr(mod, "_exec_code", lambda *a, **kw: {"result": "lauf\n", "files": []})
    client = TestClient(mod.app)

    for _ in range(2):
        r = client.post("/calculate", json={"expression": "2 + 3"})
        assert r.status_code == 200
        assert r.json()["result"] == "Ergebnis: 5"
    assert mod._CLAIMED is False
    assert events == []

    #--- Der Code-Lauf danach beansprucht den Container ganz normal.
    assert client.post("/execute", json={"code": "print(1)"}).status_code == 200
    assert events == ["retire"]
    #--- Auch beansprucht bleibt /calculate bis zum Ende erreichbar.
    r = client.post("/calculate", json={"expression": "2 * 4"})
    assert r.status_code == 200 and r.json()["result"] == "Ergebnis: 8"
    assert events == ["retire"]


def test_one_run_retire_once_with_fallback_timer(one_run):
    mod, events = one_run
    assert mod._claim_container() is True
    assert mod._claim_container() is False
    timer = mod._FALLBACK_TIMER
    assert timer is not None and timer.daemon
    #--- Hintergrundaufgabe und Sicherheitsnetz: _retire trotzdem nur einmal.
    mod._retire_once()
    mod._retire_once()
    assert events == ["retire"]
    timer.join(timeout=2)
    assert not timer.is_alive()


def test_without_one_run_switch_execute_stays_reusable(monkeypatch):
    from fastapi.testclient import TestClient
    mod = _sandbox_mod()
    monkeypatch.delenv("SANDBOX_ONE_RUN", raising=False)
    monkeypatch.setattr(mod, "_CLAIMED", False)
    retired = []
    monkeypatch.setattr(mod, "_retire", lambda: retired.append(1))
    monkeypatch.setattr(mod, "_exec_code", lambda code, *a, **kw: {"result": code + "\n", "files": []})
    client = TestClient(mod.app)

    for i in range(3):
        r = client.post("/execute", json={"code": f"print({i})"})
        assert r.status_code == 200
        assert r.json()["result"] == f"print({i})\n"
    assert mod._CLAIMED is False
    assert retired == []
