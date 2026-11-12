import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("PII_BLOCKLIST_PATH", "/nonexistent/pii_blocklist.txt")

from rag_backend import prompts


def test_render_blocks_and_owner():
    text = ("Hallo {{OWNER}}."
            "{{#tool:cloud_ask}} Cloud{{/tool:cloud_ask}}"
            "{{^tool:cloud_ask}} ohne Cloud{{/tool:cloud_ask}}")
    assert prompts.render(text, tool_names=set()) == "Hallo the user. ohne Cloud"
    assert prompts.render(text, tool_names=set(), owner="  ") == "Hallo the user. ohne Cloud"
    assert prompts.render(text, tool_names={"cloud_ask"}, owner="Anna Schmidt") == "Hallo Anna Schmidt. Cloud"


def test_owner_no_longer_comes_from_the_environment(monkeypatch):
    #--- Frueher ein Name fuer die ganze Installation (OWNER_NAME). Jetzt die
    #--- angemeldete Person, sonst "the user".
    monkeypatch.setenv("OWNER_NAME", "Zentral")
    assert prompts.render("{{OWNER}}", tool_names=set()) == "the user"


def test_old_host_tool_blocks_in_own_files_are_dropped():
    #--- Eigene Prompt-Dateien aus der Zeit mit Host-Werkzeugen: der Abschnitt faellt
    #--- weg, der Gegen-Abschnitt bleibt. Es gibt keinen Schalter mehr, der ihn einblendet.
    text = "A{{#host_tools}} SSH{{/host_tools}}{{^host_tools}} kein PC{{/host_tools}}"
    assert prompts.render(text, tool_names={"run_powershell"}) == "A kein PC"


def test_workspace_file_overrides_default(tmp_path):
    (tmp_path / "SOUL.md").write_text("eigene Seele", encoding="utf-8")
    assert prompts.load("SOUL.md", tmp_path) == ("eigene Seele", "workspace")
    text, origin = prompts.load("SKILL.md", tmp_path)
    assert origin == "vorgabe" and "document_search" in text
    assert prompts.load("GIBTS_NICHT.md", tmp_path) == ("", "keine")


def test_defaults_describe_no_host_access(tmp_path):
    for name in ("SOUL.md", "SKILL.md", "AGENTS.md"):
        raw, origin = prompts.load(name, tmp_path)
        assert origin == "vorgabe"
        assert "host_tools" not in raw, name
        for gone in ("SSH", "account `argus`", "You control the Windows PC"):
            assert gone not in raw, (name, gone)
    soul = prompts.render(prompts.load("SOUL.md", tmp_path)[0], tool_names=set())
    assert "no access" in soul and "{{" not in soul
    skill = prompts.render(prompts.load("SKILL.md", tmp_path)[0], tool_names={"document_search"})
    assert "run_powershell" not in skill and "cloud_ask" not in skill and "{{" not in skill


def test_get_prompt_uses_defaults_without_workspace_files(tmp_path, monkeypatch):
    from rag_backend import agent

    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    text = agent.get_prompt_from_env("SYSTEM_PROMPT", "")
    assert "<personality>" in text and "<tool_guide>" in text and "<workflow>" in text
    assert "SSH" not in text and "{{" not in text
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "SOUL.md").write_text("Du bist Test-Argus fuer {{OWNER}}.", encoding="utf-8")
    assert "Du bist Test-Argus fuer the user." in agent.get_prompt_from_env("SYSTEM_PROMPT", "")
    #--- Mit Konto steht dort die angemeldete Person.
    monkeypatch.setattr(agent, "_account_display_name", lambda uid: "Ada Lovelace" if uid == 7 else None)
    assert "Du bist Test-Argus fuer Ada Lovelace." in agent.get_prompt_from_env("SYSTEM_PROMPT", "", user_id=7)


def test_anrede_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("ARGUS_ANREDE", raising=False)
    assert prompts.render("Use {{ANREDE}}.", tool_names=set()) == 'Use the informal "du".'
    monkeypatch.setenv("ARGUS_ANREDE", " Sie ")
    assert prompts.render("Use {{ANREDE}}.", tool_names=set()) == 'Use the formal "Sie".'
    #--- Unbekannte Werte fallen auf du zurueck, statt den Platzhalter stehen zu lassen.
    monkeypatch.setenv("ARGUS_ANREDE", "ihr")
    assert prompts.render("Use {{ANREDE}}.", tool_names=set()) == 'Use the informal "du".'


def test_defaults_keep_internal_details_out_of_the_web(tmp_path):
    soul = prompts.render(prompts.load("SOUL.md", tmp_path)[0], tool_names={"web_search"})
    assert "<confidentiality>" in soul and "Never search the web for a file name" in soul
    assert "JARVIS" not in soul and "{{" not in soul
    skill = prompts.render(prompts.load("SKILL.md", tmp_path)[0], tool_names={"document_search"})
    assert "A file name is never a web search" in skill
