import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import re
from pathlib import Path

import pytest

#--- Fixtures aus test_main wiederverwenden: Import registriert sie hier.
from rag_backend.tests.test_main import (  # noqa: F401
    _make_account,
    override_get_db,
    test_client,
)
from rag_backend import main as backend

STATIC = Path(backend.__file__).resolve().parent / "static"


# ---- Auslieferung der Browser-Oberflaeche -----------------------------------

def test_root_leads_to_the_dashboard(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard/"


def test_dashboard_page_is_served_and_revalidated(client):
    r = client.get("/dashboard/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    #--- Dateinamen bleiben nach einem Update gleich: ohne Pruefung zeigte der
    #--- Browser weiter die alte Fassung.
    assert r.headers["cache-control"] == "no-cache"
    assert "Content-Security-Policy" in r.headers
    assert r.headers["x-frame-options"] == "DENY"


def test_every_referenced_asset_exists(client):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = set(re.findall(r'(?:src|href)="((?:js/|argus)[^"?#]+)', html))
    assert {"js/common.js", "js/auth.js", "js/chat.js", "argus.css"} <= refs
    for ref in sorted(refs):
        r = client.get(f"/dashboard/{ref}")
        assert r.status_code == 200, ref
        assert r.headers["cache-control"] == "no-cache", ref


def test_fonts_have_a_font_content_type(client):
    fonts = sorted((STATIC / "fonts").glob("*.woff2"))
    assert fonts, "Schriften fehlen in rag_backend/static/fonts"
    r = client.get(f"/dashboard/fonts/{fonts[0].name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"


def test_dashboard_does_not_serve_files_outside_static(client):
    for path in ("/dashboard/../main.py", "/dashboard/%2e%2e/main.py",
                 "/dashboard/..%2fmain.py", "/dashboard/js/../../main.py"):
        r = client.get(path)
        assert r.status_code in (400, 404), path
        assert "FastAPI(" not in r.text


def test_page_loads_nothing_from_other_hosts():
    #--- Die Seite laedt Skripte, Schriften und Stile nur vom eigenen Backend.
    for f in [STATIC / "index.html", STATIC / "argus.css", STATIC / "argus-glass.css",
              *sorted((STATIC / "js").glob("*.js"))]:
        text = f.read_text(encoding="utf-8")
        hits = re.findall(r'''(?:src|href)\s*=\s*["'](https?:)?//[^"']+|url\(\s*["']?https?://|@import\s+["']https?://''', text)
        assert not hits, f"{f.name} verweist auf einen fremden Host"


# ---- Ablaufverfolgung: Adresse aus der Einstellung ----------------------------

def test_config_reports_the_traces_address(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    #--- ops.js liest genau diesen Pfad: config.traces_url.
    cfg = client.get("/v1/dashboard/config").json()
    assert cfg["config"]["traces_url"] == backend._TRACES_URL


@pytest.mark.parametrize("raw, expected", [
    ("http://127.0.0.1:6006", "http://127.0.0.1:6006"),
    ("https://argus.example.org/phoenix/", "https://argus.example.org/phoenix"),
    ("  http://server:6006  ", "http://server:6006"),
    ("", ""),
])
def test_traces_url_accepts_plain_addresses(raw, expected):
    assert backend._clean_traces_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://x; script-src *",          # weitere CSP-Direktive
    "http://x http://boese.example",   # zweite Quelle
    "javascript:alert(1)",
    "http://x/'unsafe-inline'",
    "ftp://x",
    "http://user:pw@x",
])
def test_traces_url_rejects_values_that_would_alter_the_csp(raw):
    assert backend._clean_traces_url(raw) == ""


def test_frame_src_follows_the_traces_address():
    assert backend._frame_src("") == "'self'"
    assert backend._frame_src("https://argus.example.org/phoenix") == "'self' https://argus.example.org"
    #--- Lokal ist Phoenix unter beiden Namen erreichbar.
    assert backend._frame_src("http://127.0.0.1:6006") == "'self' http://127.0.0.1:6006 http://localhost:6006"


# ---- Erstes Konto: Einrichtungs-Schluessel ------------------------------------

def test_state_says_whether_setup_needs_a_token_but_never_the_token(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_SETUP_TOKEN", raising=False)
    assert client.get("/v1/dashboard/auth/state").json()["setup_token_required"] is False
    monkeypatch.setenv("DASHBOARD_SETUP_TOKEN", "einmal-geheim-0123456789")
    r = client.get("/v1/dashboard/auth/state")
    assert r.json()["setup_token_required"] is True
    assert "einmal-geheim" not in r.text


def test_state_drops_the_setup_hint_once_an_account_exists(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_SETUP_TOKEN", raising=False)
    _make_account(client, "chef", "einLangesPasswort", "admin")
    client.cookies.clear()
    monkeypatch.setenv("DASHBOARD_SETUP_TOKEN", "einmal-geheim-0123456789")
    state = client.get("/v1/dashboard/auth/state").json()
    assert state["needs_setup"] is False
    assert "setup_token_required" not in state


def test_page_offers_a_field_for_the_setup_token():
    #--- Ohne das Feld liesse sich eine Installation mit Schluessel im Browser
    #--- nicht einrichten.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    auth = (STATIC / "js" / "auth.js").read_text(encoding="utf-8")
    assert 'id="auth-token"' in html
    assert "X-Setup-Token" in auth and "setup_token_required" in auth
