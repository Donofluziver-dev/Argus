import base64
import os
import tempfile
import pytest

try:
    from calc_sandbox.app import _run, _exec_code, _EXEC_LOCK
except ModuleNotFoundError:
    from app import _run, _exec_code, _EXEC_LOCK

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

