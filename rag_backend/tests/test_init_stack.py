"""Einrichtung ohne Zusatzprogramme: setup/init_stack.py legt Geheimnisse und .env an."""
import importlib.util
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_SCRIPT = Path(__file__).resolve().parents[2] / "setup" / "init_stack.py"
#--- Im Image und beim Lauf nur mit eingebundenem rag_backend/ fehlt der Ordner setup/.
pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="setup/init_stack.py nicht eingebunden")


@pytest.fixture(scope="module")
def init_stack():
    spec = importlib.util.spec_from_file_location("init_stack", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_acl(_dir):
    return False


TEMPLATE = (
    "# Vorlage\r\n"
    "ARGUS_MODELS_DIR=C:/Argus_Models\r\n"
    "ARGUS_WORKSPACE_DIR=C:/Argus_Workspace\r\n"
    "SEARXNG_LANGUAGE=\r\n"
    "# Geheimnisse\r\n"
    "SEARXNG_SECRET=\r\n"
    "CANARY_TOKENS=\r\n"
    "DASHBOARD_SETUP_TOKEN=\r\n"
    "EGRESS_AFTER_SENSITIVE=block\r\n"
)


def _stack(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env.example").write_bytes(TEMPLATE.encode("utf-8"))
    return tmp_path


# ---- Geheimnisse in API_Tokens/ ---------------------------------------------

def test_secrets_are_created_and_decryptable(init_stack, tmp_path):
    files, _ = init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    key = (tmp_path / "fernet_key.txt").read_text(encoding="utf-8")
    assert init_stack.is_valid_fernet_key(key)
    for name in ("llm_api_key.txt", "jwt_secret.txt", "audit_hmac_key.txt"):
        raw = (tmp_path / name).read_text(encoding="utf-8")
        assert raw.startswith("ENC:") and "\n" not in raw
        plain = Fernet(key.encode()).decrypt(raw[4:].encode()).decode()
        assert len(plain) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", plain)
        assert files[name] == "angelegt"
    #--- Das Backend lehnt einen JWT-Schluessel unter 32 Zeichen ab.
    jwt_plain = Fernet(key.encode()).decrypt(
        (tmp_path / "jwt_secret.txt").read_text(encoding="utf-8")[4:].encode()).decode()
    assert len(jwt_plain) >= 32
    #--- Postgres liest sein Passwort direkt, also Klartext.
    pg = (tmp_path / "postgres_password.txt").read_text(encoding="utf-8")
    assert not pg.startswith("ENC:") and len(pg) >= 24


def test_placeholders_exist_as_empty_files(init_stack, tmp_path):
    #--- Fehlt die Datei, legt Docker an ihrer Stelle einen Ordner an.
    files, _ = init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    for name in ("Gemini.txt", "LANGSMITH_API_KEY.txt"):
        p = tmp_path / name
        assert p.is_file() and p.read_bytes() == b""
        assert files[name] == "Platzhalter"


def test_placeholders_cover_every_file_the_compose_files_mount(init_stack):
    root = _SCRIPT.parent.parent
    mounted = set()
    mounted |= set(re.findall(r"\./API_Tokens/([A-Za-z0-9_.]+):",
                              (root / "docker-compose.yml").read_text(encoding="utf-8")))
    created = ({init_stack.FERNET_KEY_FILE} | {g[0] for g in init_stack.GENERATED}
               | set(init_stack.OPTIONAL_PLACEHOLDERS))
    assert mounted, "keine Einbindungen aus API_Tokens/ gefunden"
    assert mounted <= created, f"wird eingebunden, aber nicht angelegt: {sorted(mounted - created)}"
    #--- Kein Container bekommt Zugangsdaten fuer einen Host.
    assert not any("ssh" in name.lower() for name in mounted | created)


def test_second_run_changes_nothing(init_stack, tmp_path):
    init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    files, _ = init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert set(files.values()) == {"behalten"}


def test_user_supplied_key_is_never_overwritten(init_stack, tmp_path):
    (tmp_path / "Gemini.txt").write_text("vom-nutzer-eingetragen", encoding="utf-8")
    files, _ = init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    assert (tmp_path / "Gemini.txt").read_text(encoding="utf-8") == "vom-nutzer-eingetragen"
    assert files["Gemini.txt"] == "behalten"


def test_broken_fernet_key_stops_instead_of_replacing(init_stack, tmp_path):
    (tmp_path / "fernet_key.txt").write_text("kaputt", encoding="utf-8")
    with pytest.raises(SystemExit):
        init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    assert (tmp_path / "fernet_key.txt").read_text(encoding="utf-8") == "kaputt"


def test_old_key_file_name_is_renamed(init_stack, tmp_path):
    (tmp_path / "sglang_api_key.txt").write_text("alter-schluessel-bleibt", encoding="utf-8")
    files, _ = init_stack.ensure_stack_secrets(tmp_path, restrict_acl=_no_acl)
    assert not (tmp_path / "sglang_api_key.txt").exists()
    assert (tmp_path / "llm_api_key.txt").read_text(encoding="utf-8") == "alter-schluessel-bleibt"
    assert files["sglang_api_key.txt"] == "umbenannt" and files["llm_api_key.txt"] == "behalten"


# ---- .env --------------------------------------------------------------------

def test_set_env_values_keeps_comments_order_and_line_endings(init_stack):
    out = init_stack.set_env_values(TEMPLATE, {"SEARXNG_LANGUAGE": "de-AT", "NEU": "1"})
    assert out.startswith("# Vorlage\r\nARGUS_MODELS_DIR=C:/Argus_Models\r\n")
    assert "\r\nSEARXNG_LANGUAGE=de-AT\r\n# Geheimnisse\r\n" in out
    assert out.endswith("EGRESS_AFTER_SENSITIVE=block\r\nNEU=1\r\n")
    assert "\n" not in out.replace("\r\n", "")


def test_values_with_special_characters_are_quoted(init_stack):
    assert init_stack.format_env_value("C:/Argus_Models") == "C:/Argus_Models"
    assert init_stack.format_env_value("Anna Maria") == "'Anna Maria'"
    assert init_stack.format_env_value("a$b#c") == "'a$b#c'"
    assert init_stack.parse_env("X='a$b#c'\n")["X"] == "a$b#c"
    for bad in ("zwei\nzeilen", "it's $x"):
        with pytest.raises(ValueError):
            init_stack.format_env_value(bad)
    with pytest.raises(ValueError):
        init_stack.set_env_values("", {"UNGUELTIG-NAME": "x"})


def test_main_creates_env_and_prints_no_secret(init_stack, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(init_stack, "restrict_to_owner", _no_acl)
    monkeypatch.setattr(init_stack.ensure_stack_secrets, "__defaults__", (_no_acl,))
    stack = _stack(tmp_path)
    ws = tmp_path / "arbeit"
    rc = init_stack.main(["--stack", str(stack), "--workspace", str(ws),
                          "--models", str(tmp_path / "modelle")])
    assert rc == 0
    out = capsys.readouterr().out
    env = init_stack.parse_env((stack / ".env").read_bytes().decode("utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", env["SEARXNG_SECRET"])
    assert env["CANARY_TOKENS"].startswith("sk-argus-") and len(env["CANARY_TOKENS"]) >= 12
    assert env["EGRESS_AFTER_SENSITIVE"] == "block"
    #--- Der Koeder liegt im Arbeitsordner und traegt genau den Wert aus der .env.
    assert env["CANARY_TOKENS"] in (ws / "zugangsdaten.txt").read_text(encoding="utf-8")
    #--- Ausgegeben werden Dateinamen, nie Werte.
    #--- Das Backend vergleicht den Einrichtungs-Schluessel woertlich mit dem Header.
    assert len(env["DASHBOARD_SETUP_TOKEN"]) >= 20
    secrets_on_disk = [env["SEARXNG_SECRET"], env["CANARY_TOKENS"], env["DASHBOARD_SETUP_TOKEN"]]
    secrets_on_disk += [p.read_text(encoding="utf-8") for p in (stack / "API_Tokens").iterdir()
                        if p.stat().st_size]
    for value in secrets_on_disk:
        assert value not in out
    assert "fernet_key.txt" in out and "angelegt" in out


def test_evals_folder_follows_a_custom_workspace(init_stack, tmp_path, monkeypatch):
    monkeypatch.setattr(init_stack.ensure_stack_secrets, "__defaults__", (_no_acl,))
    stack = _stack(tmp_path)
    tmpl = stack / ".env.example"
    tmpl.write_bytes(tmpl.read_bytes() + b"ARGUS_EVALS_DIR=C:/Argus_Workspace/Evals\r\n")
    ws = (tmp_path / "arbeit").as_posix()
    assert init_stack.main(["--stack", str(stack), "--workspace", ws]) == 0
    env = init_stack.parse_env((stack / ".env").read_bytes().decode("utf-8"))
    #--- Sonst laegen die Eval-Laeufe weiter im Standardordner eines anderen Laufwerks.
    assert env["ARGUS_EVALS_DIR"] == f"{ws}/Evals"
    #--- Ein eigens gesetzter Ordner bleibt bei einem spaeteren Lauf stehen.
    text = (stack / ".env").read_bytes().decode("utf-8")
    (stack / ".env").write_bytes(
        init_stack.set_env_values(text, {"ARGUS_EVALS_DIR": "D:/Messungen"}).encode("utf-8"))
    assert init_stack.main(["--stack", str(stack), "--workspace", ws]) == 0
    env = init_stack.parse_env((stack / ".env").read_bytes().decode("utf-8"))
    assert env["ARGUS_EVALS_DIR"] == "D:/Messungen"


def test_main_keeps_existing_env_secrets(init_stack, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(init_stack.ensure_stack_secrets, "__defaults__", (_no_acl,))
    stack = _stack(tmp_path)
    args = ["--stack", str(stack), "--workspace", str(tmp_path / "arbeit")]
    assert init_stack.main(args) == 0
    first = (stack / ".env").read_bytes()
    capsys.readouterr()
    assert init_stack.main(args) == 0
    assert (stack / ".env").read_bytes() == first
    assert ".env unveraendert" in capsys.readouterr().out


def test_main_refuses_a_folder_that_is_not_an_installation(init_stack, tmp_path, capsys):
    assert init_stack.main(["--stack", str(tmp_path)]) == 1
    assert not (tmp_path / "API_Tokens").exists()
    assert "kein Argus-Ordner" in capsys.readouterr().err
