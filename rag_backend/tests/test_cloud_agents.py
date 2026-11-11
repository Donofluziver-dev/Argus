# Offline-Unit-Tests fuer die Cloud-Agenten: Redactor, Tripwire, Rate-Limiter,
# Provider-Auswahl und Mission-Engine (alles gemockt, kein Netz).
# Env-Variablen NACH den Imports setzen (FERNET_KEY-Trap).
import asyncio
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["SKIP_MIGRATIONS"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///./test_cloud.db"
os.environ["FERNET_KEY"] = "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw="
os.environ["CLOUD_REDACTION_LLM_PASS"] = "false"
os.environ["CLOUD_MISSION_PLANNER"] = "gemini"


from rag_backend.cloud_agents import providers as providers_mod
from rag_backend.cloud_agents import redactor
from rag_backend.cloud_agents.providers import (
    CloudNotConfigured,
    CloudRateLimited,
    RollingLimiter,
    get_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    providers_mod._PROVIDERS.clear()
    yield
    providers_mod._PROVIDERS.clear()


@pytest.fixture()
def blocklist(tmp_path, monkeypatch):
    path = tmp_path / "pii_blocklist.txt"
    path.write_text("# Kommentar\nMax Mustermann\nMusterstrasse 12\n", encoding="utf-8")
    monkeypatch.setenv("PII_BLOCKLIST_PATH", str(path))
    redactor._BLOCKLIST_CACHE.update({"path": None, "mtime": None, "terms": []})
    return path


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------

def test_redactor_roundtrip(blocklist):
    text = (
        "Max Mustermann wohnt in der Musterstrasse 12, erreichbar unter "
        "max@example.com oder 0171 1234567, IBAN DE89370400440532013000, geb. 01.02.1990."
    )
    red, mapping = redactor.redact_deterministic(text)
    assert "Max Mustermann" not in red
    assert "Musterstrasse 12" not in red
    assert "max@example.com" not in red
    assert "0171 1234567" not in red
    assert "DE89370400440532013000" not in red
    assert "01.02.1990" not in red
    assert "[PII_1]" in red
    assert redactor.rehydrate(red, mapping) == text


def test_redactor_stable_placeholders(blocklist):
    red1, mapping = redactor.redact_deterministic("Max Mustermann war da.")
    red2, mapping = redactor.redact_deterministic("Nochmal Max Mustermann.", mapping)
    ph1 = [k for k in mapping if mapping[k] == "Max Mustermann"]
    assert len(ph1) == 1
    assert ph1[0] in red1 and ph1[0] in red2


def test_tripwire_blocklist(blocklist):
    assert redactor.tripwire("Bericht ueber max mustermann und KI") is not None
    assert redactor.tripwire("Bericht ueber KI-Trends 2026") is None


def test_tripwire_email_and_iban(blocklist):
    assert redactor.tripwire("Kontakt: foo@bar.de") is not None
    assert redactor.tripwire("Konto DE89370400440532013000") is not None


# ---------------------------------------------------------------------------
# Qwen-NER-Pass (LLM gemockt) -- die tragende generische Schicht
# ---------------------------------------------------------------------------

def test_ner_replaces_unknown_person(monkeypatch):
    # Unbekannte Namen (nicht in irgendeiner Liste) werden erkannt und ersetzt.
    monkeypatch.setenv("CLOUD_REDACTION_LLM_PASS", "true")

    async def fake_ner(text):
        return '[{"value": "Erika Beispiel", "category": "PERSON"}, {"value": "Beispiel GmbH", "category": "ORG"}]'

    monkeypatch.setattr(redactor, "_ask_ner", fake_ner)
    text = "Pruefe den Lebenslauf von Erika Beispiel (Beispiel GmbH) auf Luecken."
    red, mapping = asyncio.run(redactor.redact_llm_ner(text))
    assert "Erika Beispiel" not in red
    assert "Beispiel GmbH" not in red
    assert "[PERSON_1]" in red and "[ORG_1]" in red
    assert redactor.rehydrate(red, mapping) == text


def test_ner_fail_closed(monkeypatch):
    # Ohne funktionierenden NER-Pass darf nichts rausgehen (Default).
    monkeypatch.setenv("CLOUD_REDACTION_LLM_PASS", "true")
    monkeypatch.setenv("CLOUD_REDACTION_FAIL_CLOSED", "true")

    async def broken(text):
        raise RuntimeError("SGLang nicht erreichbar")

    monkeypatch.setattr(redactor, "_ask_ner", broken)
    with pytest.raises(redactor.RedactionUnavailable):
        asyncio.run(redactor.redact_llm_ner("Text ueber Max Beispiel"))


def test_ner_fail_open_when_configured(monkeypatch):
    monkeypatch.setenv("CLOUD_REDACTION_LLM_PASS", "true")
    monkeypatch.setenv("CLOUD_REDACTION_FAIL_CLOSED", "false")

    async def broken(text):
        raise RuntimeError("SGLang nicht erreichbar")

    monkeypatch.setattr(redactor, "_ask_ner", broken)
    red, mapping = asyncio.run(redactor.redact_llm_ner("Unveraenderter Text"))
    assert red == "Unveraenderter Text"
    assert mapping == {}


# ---------------------------------------------------------------------------
# Rate-Limiter / Provider-Auswahl
# ---------------------------------------------------------------------------

def test_rolling_limiter_rpm():
    lim = RollingLimiter(rpm=2, rpd=100)
    lim.record(now=0.0)
    lim.record(now=1.0)
    assert lim.seconds_until_slot(now=2.0) > 0
    assert lim.seconds_until_slot(now=61.0) == 0


def test_rolling_limiter_rpd():
    lim = RollingLimiter(rpm=100, rpd=2)
    lim.record(now=0.0)
    lim.record(now=1.0)
    assert lim.seconds_until_slot(now=2.0) > 3600


def test_per_model_limiter_and_key_rotation():
    # Der Limiter ist pro (Key-Index, Modell) getrennt und wendet die
    # modell-spezifischen Free-Tier-Limits an.
    p = providers_mod.GeminiProvider()
    lim_lite = p._get_limiter(0, "gemini-3.1-flash-lite")   # rpd 500
    lim_flash = p._get_limiter(0, "gemini-3.5-flash")       # rpd 20
    assert lim_lite is not lim_flash
    assert lim_lite.rpd == 500 and lim_flash.rpd == 20
    # Idempotent: gleicher Key+Modell -> derselbe Limiter (Zustand ueberlebt).
    assert p._get_limiter(0, "gemini-3.5-flash") is lim_flash
    # 3-Key-Rotation: anderer Key-Index -> getrennter Limiter.
    assert p._get_limiter(1, "gemini-3.5-flash") is not lim_flash
    # Unbekanntes Modell faellt auf die Provider-Defaults zurueck.
    lim_unknown = p._get_limiter(0, "irgendwas-neues")
    assert lim_unknown.rpm == p.default_rpm and lim_unknown.rpd == p.default_rpd
    # Erschoepftes Modell blockt, das RPD-starke bleibt frei.
    for _ in range(20):
        lim_flash.record()
    assert lim_flash.seconds_until_slot() > 0
    assert lim_lite.seconds_until_slot() == 0


def test_provider_not_configured(monkeypatch):
    monkeypatch.setattr(providers_mod.CloudProvider, "_api_key", lambda self: "")
    with pytest.raises(CloudNotConfigured):
        get_provider()


def test_provider_single_gemini(monkeypatch):
    '''Gemini ist der einzige Provider -- ein unbekannter Wunsch-Provider faellt
    auf ihn zurueck, statt einen KeyError zu werfen.'''
    monkeypatch.setattr(providers_mod.GeminiProvider, "_api_key", lambda self: "test-key")
    assert get_provider().name == "gemini"
    assert get_provider("gibtsnicht").name == "gemini"


# ---------------------------------------------------------------------------
# Mission-Engine (Happy Path, alles gemockt)
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.calls = []    # protokolliert (rolle, grounding) je generate-Aufruf
        self.models = []   # verwendetes Modell je Call (Pro-Rollen-Modellwahl)
        self.prompts = []  # Payloads (fuer RESEARCH-MATERIAL-Asserts)
        self.plan_json = '["Recherchiere Grundlagen", "Fasse zusammen"]'

    async def generate(self, system, prompt, max_tokens, grounding=False, model=None):
        # Rollen-Erkennung ueber die englischen System-Prompt-Marker
        if "mission planner" in system:
            role = "planner"
        elif "quality reviewer" in system:
            role = "reviewer"
        else:
            role = "worker"
        self.calls.append((role, grounding))
        self.models.append(model)
        self.prompts.append(prompt)
        if role == "planner":
            return self.plan_json, 5, 7
        if role == "reviewer":
            return "ACCEPT", 11, 22
        return "Recherche-Ergebnis fuer [PII_1].", 11, 22


def test_mission_happy_path(tmp_path, monkeypatch, blocklist):
    from rag_backend import models
    from rag_backend.cloud_agents import missions

    db_path = tmp_path / "missions.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)

    fake = FakeProvider()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions.providers_mod, "get_fallback", lambda name: None)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    appended = []

    def fake_append(session_id, text):
        appended.append(text)
        return True

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            return '["Recherchiere Grundlagen", "Fasse zusammen"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL: kombiniertes Ergebnis fuer [PII_1]."

    monkeypatch.setattr(missions, "_append_dashboard_message", fake_append)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    goal = "Erstelle einen Bericht fuer Max Mustermann ueber KI-Trends."
    red_goal, mapping = redactor.redact_deterministic(goal)
    assert "Max Mustermann" not in red_goal

    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "dashboard", dashboard_session_id=42)
    asyncio.run(missions._run_mission(mid))

    data = missions._load(mid)
    assert data["status"] == "done"
    result = missions.get_result_text(mid)
    assert result is not None
    assert "Max Mustermann" in result  # Re-Hydration hat gegriffen
    assert "[PII_1]" not in result

    # Standard-Rollen: Gemini plant, dazu zwei grounded Reviewer.
    assert ("planner", False) in fake.calls          # Planung lief in der Cloud
    assert any(role == "reviewer" and g for role, g in fake.calls)  # Reviewer grounded
    # Geprueft wird gegen die KETTEN, nicht gegen feste Modellnamen --
    # die Rollentrennung ist das, was gelten muss.
    planner_head = missions._planner_models()[0]
    worker_head = missions._worker_models()[0]
    assert planner_head != worker_head, "Planner und Worker duerfen nicht dasselbe Modell sein"
    assert fake.models[0] == planner_head          # erster Call ist die Planung
    assert worker_head in fake.models              # Worker/Reviewer auf der Worker-Kette
    brief = missions.list_missions_brief()
    row = next(r for r in brief if r["id"] == mid)
    assert row["tokens_in"] == 27 and row["tokens_out"] == 51
    assert appended and "27/51 Tokens" in appended[-1]

    engine.dispose()


def test_mission_planner_local(tmp_path, monkeypatch, blocklist):
    # CLOUD_MISSION_PLANNER=local -> Planung bleibt lokal, nur Worker-Calls.
    from rag_backend import models
    from rag_backend.cloud_agents import missions

    engine = create_engine(f"sqlite:///{tmp_path / 'mpl.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)
    monkeypatch.setenv("CLOUD_MISSION_PLANNER", "local")

    fake = FakeProvider()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions.providers_mod, "get_fallback", lambda name: None)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            return '["Teilaufgabe A", "Teilaufgabe B"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini")
    asyncio.run(missions._run_mission(mid))

    assert missions._load(mid)["status"] == "done"
    assert fake.calls and all(role == "reviewer" for role, _ in fake.calls)
    row = next(r for r in missions.list_missions_brief() if r["id"] == mid)
    assert row["tokens_in"] == 22 and row["tokens_out"] == 44

    engine.dispose()


def test_mission_planner_cloud_fallback(tmp_path, monkeypatch, blocklist):
    # Cloud-Planner faellt aus -> harter Fallback auf lokalen Planner, Mission laeuft.
    from rag_backend import models
    from rag_backend.cloud_agents import missions

    engine = create_engine(f"sqlite:///{tmp_path / 'mpf.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)
    monkeypatch.setenv("CLOUD_MISSION_PLANNER", "gemini")

    class FailPlanner(FakeProvider):
        async def generate(self, system, prompt, max_tokens, grounding=False, model=None):
            if "mission planner" in system:
                raise RuntimeError("cloud planner down")
            return await FakeProvider.generate(self, system, prompt, max_tokens, grounding, model)

    fake = FailPlanner()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions.providers_mod, "get_fallback", lambda name: None)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    planner_local = {"used": False}

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            planner_local["used"] = True
            return '["Nur eine Teilaufgabe"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini")
    asyncio.run(missions._run_mission(mid))

    assert missions._load(mid)["status"] == "done"
    assert planner_local["used"] is True
    assert all(role == "reviewer" for role, _ in fake.calls)

    engine.dispose()


def test_cloud_ask_grounding(monkeypatch, blocklist):
    # cloud_ask groundet standardmaessig -> Provider bekommt grounding=True.
    from rag_backend.cloud_agents import missions

    fake = FakeProvider()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)
    monkeypatch.setenv("CLOUD_ASK_GROUNDING", "true")

    out = asyncio.run(missions.run_cloud_ask("Was ist aktuell neu bei KI?", "", ""))
    assert fake.calls == [("worker", True)]
    assert "Recherche-Ergebnis" in out


def test_parse_plan_formats():
    from rag_backend.cloud_agents import missions

    # Neues Objekt-Format {task, search}
    plan = missions._parse_plan(
        '[{"task": "Versionen ermitteln", "search": "python stable release"},'
        ' {"task": "Bericht schreiben", "search": ""}]', "Fallback"
    )
    assert plan[0] == {"task": "Versionen ermitteln", "search": "python stable release"}
    assert plan[1]["search"] == ""
    # Alt-Format (lokaler Qwen-Planner): reine Strings -> search leer
    plan = missions._parse_plan('["Aufgabe A", "Aufgabe B"]', "Fallback")
    assert plan == [{"task": "Aufgabe A", "search": ""}, {"task": "Aufgabe B", "search": ""}]
    # Muell -> Gesamtziel als eine Teilaufgabe
    assert missions._parse_plan("kein json", "Fallback") == [{"task": "Fallback", "search": ""}]


def test_mission_research_material(tmp_path, monkeypatch, blocklist):
    # Planner mit search-Query loest lokale Recherche aus, ohne Query nicht.
    from rag_backend import models
    from rag_backend.cloud_agents import missions

    engine = create_engine(f"sqlite:///{tmp_path / 'mrm.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)

    fake = FakeProvider()
    fake.plan_json = (
        '[{"task": "Ermittle die aktuelle Version", "search": "python version aktuell"},'
        ' {"task": "Fasse zusammen", "search": ""}]'
    )
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions.providers_mod, "get_fallback", lambda name: None)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    research_calls = []

    async def fake_research(query):
        research_calls.append(query)
        return "Quelle: Python 3.14.6 erschienen 2026-06-10 (https://python.org)"

    local_prompts = []

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        local_prompts.append((system, prompt))
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_local_research", fake_research)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini")
    asyncio.run(missions._run_mission(mid))

    assert missions._load(mid)["status"] == "done"
    assert research_calls == ["python version aktuell"]  # nur die markierte Teilaufgabe
    worker_prompts = [p for sys, p in local_prompts if "work agent" in sys]
    assert any("RESEARCH MATERIAL" in p and "python.org" in p for p in worker_prompts)
    assert any("RESEARCH MATERIAL" not in p for p in worker_prompts)  # 2. Teilaufgabe ohne

    engine.dispose()


def test_generate_chain_fallback():
    # Erstes Modell der Kette limitiert -> zweites uebernimmt; used_model stimmt.
    from rag_backend.cloud_agents import missions

    class ChainStub:
        def __init__(self):
            self.models = []

        async def generate(self, system, prompt, max_tokens, grounding=False, model=None):
            self.models.append(model)
            if model == "gemini-3.1-flash-lite":
                raise CloudRateLimited("RPD erschoepft", 60.0)
            return "Antwort von " + str(model), 3, 4

    stub = ChainStub()
    text, ti, to, used = asyncio.run(missions._generate_chain(
        stub, "sys", "prompt", 100, False, ["gemini-3.1-flash-lite", "gemma-4-31b-it"]
    ))
    assert used == "gemma-4-31b-it"
    assert "gemma-4-31b-it" in text
    assert stub.models == ["gemini-3.1-flash-lite", "gemma-4-31b-it"]


def test_mission_tripwire_blocks(tmp_path, monkeypatch, blocklist):
    from rag_backend import models
    from rag_backend.cloud_agents import missions

    db_path = tmp_path / "missions2.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)

    fake = FakeProvider()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        return '["Teilaufgabe"]'

    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    # Ungefiltertes Ziel direkt einschleusen -> Tripwire muss den Versand blocken.
    mid = missions.create_mission(0, "Bericht ueber Max Mustermann", "", {}, "gemini")
    asyncio.run(missions._run_mission(mid))

    data = missions._load(mid)
    assert data["status"] == "failed"
    with factory() as db:
        m = db.get(models.Mission, mid)
        assert "Tripwire" in (m.error or "")

    engine.dispose()


def test_credentials_never_leave_the_host():
    '''Missionen laufen bewusst mit CLOUD_MISSION_AUTO_APPROVE=true -- niemand sieht das
    Paket vorher. Ein anonymisierter Vertrag darf raus, Zugangsdaten nie: sie sind
    pseudonymisiert wertlos und im Zweifel der eigentliche Schaden. Deshalb doppelt --
    Ersetzung in redact_deterministic UND harter Stopp im Tripwire.'''
    from rag_backend.cloud_agents import redactor

    secrets_in_text = [
        "password: hunter2trotzdemlang",
        "API_KEY=sk-abcdefghijklmnopqrstuvwx",
        "hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "connection_string: Server=db;Pwd=GeheimGeheim",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef\n-----END OPENSSH PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
    ]
    for raw in secrets_in_text:
        red, mapping = redactor.redact_deterministic(raw)
        assert "SECRET" in red, raw
        # Der Wert selbst ist weg ...
        assert all(v not in red for v in mapping.values()), raw
        # ... und der Tripwire laesst das Ergebnis passieren (kein Selbsttreffer).
        assert redactor.tripwire(red) is None, red
        # Ungefiltert dagegen: harter Stopp.
        assert redactor.tripwire(raw) is not None, raw

    # Die Benennung bleibt lesbar -- ein Konfig-Ausschnitt bleibt verstaendlich.
    red, _ = redactor.redact_deterministic("password: hunter2trotzdemlang")
    assert red.lower().startswith("password:")

    # Fachlicher Text ohne Zugangsdaten bleibt unangetastet.
    vertrag = "Der Auftragnehmer liefert bis zum 31.12. eine Analyse der Marktlage."
    red, _ = redactor.redact_deterministic(vertrag)
    assert red == vertrag
    assert redactor.tripwire(red) is None


def _mission_delivery_env(tmp_path, monkeypatch, dbname):
    from rag_backend import models
    from rag_backend.cloud_agents import missions
    engine = create_engine(f"sqlite:///{tmp_path / dbname}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(missions, "_SESSION_FACTORY", factory)
    fake = FakeProvider()
    monkeypatch.setattr(missions.providers_mod, "get_provider", lambda preferred=None: fake)
    monkeypatch.setattr(missions.providers_mod, "get_fallback", lambda name: None)
    monkeypatch.setattr(missions, "_write_audit", lambda **kw: None)

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)
    return engine, missions


def test_mission_delivery_dashboard_append(tmp_path, monkeypatch, blocklist):
    # Dashboard-Start -> Ergebnis wird in die Chat-Session geschrieben.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mdd.db")
    appended = []

    def fake_append(session_id, text):
        appended.append((session_id, text))
        return True

    monkeypatch.setattr(missions, "_append_dashboard_message", fake_append)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "dashboard", dashboard_session_id=42)
    asyncio.run(missions._run_mission(mid))

    assert missions._load(mid)["status"] == "done"
    assert appended and appended[-1][0] == 42
    assert "Mission" in appended[-1][1]
    engine.dispose()


def test_mission_delivery_without_session_keeps_result(tmp_path, monkeypatch, blocklist):
    # Scheitert die Zustellung (Session geloescht), bleibt das Ergebnis abrufbar.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mdf.db")

    def fake_append(session_id, text):
        return False

    monkeypatch.setattr(missions, "_append_dashboard_message", fake_append)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "dashboard", dashboard_session_id=42)
    asyncio.run(missions._run_mission(mid))
    assert missions._load(mid)["status"] == "done"
    assert missions.get_result_text(mid)
    engine.dispose()


def test_deliver_skips_when_followed(tmp_path, monkeypatch, blocklist):
    # Aktiver Follow-Stream -> _deliver ueberspringt die Zustellung.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl1.db")
    appended = []

    def fake_append(session_id, text):
        appended.append(text)
        return True

    monkeypatch.setattr(missions, "_append_dashboard_message", fake_append)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "dashboard", dashboard_session_id=42)
    missions._FOLLOWED.add(mid)
    try:
        asyncio.run(missions._run_mission(mid))
    finally:
        missions._FOLLOWED.discard(mid)
    assert missions._load(mid)["status"] == "done"
    assert not appended
    engine.dispose()


def test_active_for_dashboard_and_step(tmp_path, monkeypatch, blocklist):
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl2.db")
    red_goal, mapping = redactor.redact_deterministic("Analyse A.")
    mid1 = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                   "dashboard", dashboard_session_id=101)
    mid2 = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                   "dashboard", dashboard_session_id=102)
    assert missions.active_for_dashboard(101) == [mid1]  # queued zaehlt als aktiv
    assert missions.mission_step(mid1)[0] == "queued"
    assert missions.mission_step(999999) == ("missing", "")
    missions._finish(mid1, "done", "FERTIG.", None, 1, 1, 1)
    assert missions.active_for_dashboard(101) == []      # terminal faellt raus
    assert missions.active_for_dashboard(102) == [mid2]
    assert missions.active_for_dashboard(None) == []
    engine.dispose()


def test_delivery_text_formats(tmp_path, monkeypatch, blocklist):
    # delivery_text ist die EINE Format-Quelle -- die Needle-Woerter muessen
    # exakt im Text stehen.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl3.db")
    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Max Mustermann.")
    m_done = missions.create_mission(0, red_goal, "", mapping, "gemini", "dashboard", dashboard_session_id=1)
    m_fail = missions.create_mission(0, red_goal, "", mapping, "gemini", "dashboard", dashboard_session_id=1)
    m_canc = missions.create_mission(0, red_goal, "", mapping, "gemini", "dashboard", dashboard_session_id=1)
    m_open = missions.create_mission(0, red_goal, "", mapping, "gemini", "dashboard", dashboard_session_id=1)
    missions._finish(m_done, "done", "ERGEBNIS X.", None, 1, 1, 1)
    missions._finish(m_fail, "failed", None, "Kaputt.")
    missions._finish(m_canc, "cancelled")

    t_done = missions.delivery_text(m_done)
    assert f"Mission {m_done} abgeschlossen" in t_done
    assert "ERGEBNIS X." in t_done
    assert "Max Mustermann" in t_done  # Ziel wurde re-hydriert
    assert f"Mission {m_fail} gescheitert" in missions.delivery_text(m_fail)
    assert "Kaputt." in missions.delivery_text(m_fail)
    assert f"Mission {m_canc} abgebrochen" in missions.delivery_text(m_canc)
    assert missions.delivery_text(m_open) is None   # nicht terminal
    assert missions.delivery_text(999999) is None   # unbekannt
    engine.dispose()
