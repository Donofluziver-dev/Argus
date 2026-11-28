import base64
from urllib.parse import quote

import pytest

from rag_backend import canary

TOKEN = "sk-argus-Q7wErTy9UiOp4AsDfGh2"


@pytest.fixture(autouse=True)
def _koeder(monkeypatch):
    monkeypatch.setenv("CANARY_TOKENS", TOKEN)
    monkeypatch.setattr(canary, "_audit", lambda where: None)


@pytest.mark.parametrize("text", [
    f"https://example.org/?q={TOKEN}",
    f"https://example.org/?q={quote(TOKEN, safe='')}",
    f"https://example.org/?q={quote(quote(TOKEN, safe=''), safe='')}",
    f"suche {TOKEN.upper()}",
    f"x={base64.b64encode(TOKEN.encode()).decode()}",
    f"x={base64.b64encode(b'a' + TOKEN.encode()).decode()}",
    f"x={base64.b64encode(b'ab' + TOKEN.encode() + b'tail').decode()}",
    f"x={base64.urlsafe_b64encode(b'zz' + TOKEN.encode()).decode()}",
    f"hex {TOKEN.encode().hex()}",
    f"rueckwaerts {TOKEN[::-1]}",
    " ".join(TOKEN),
    "-".join(TOKEN),
    f"&#115;&#107;{TOKEN[2:]}",
])
def test_encoded_canary_is_detected(text):
    assert canary.check_canary(text, "Test") is True


@pytest.mark.parametrize("text", [
    "Wie hoch ist die Kilometerpauschale 2025?",
    "https://example.org/search?q=planetengetriebe+pg3+wartung",
    base64.b64encode(b"ganz normaler inhalt ohne koeder, etwas laenger").decode(),
    "sk-argus-ANDERERWERT1234567890",
])
def test_ordinary_text_is_not_flagged(text):
    assert canary.check_canary(text, "Test") is False


def test_no_tokens_configured(monkeypatch):
    monkeypatch.setenv("CANARY_TOKENS", "")
    assert canary.check_canary(TOKEN, "Test") is False
