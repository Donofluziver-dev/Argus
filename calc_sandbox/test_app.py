import pytest
from calc_sandbox.app import _run


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
