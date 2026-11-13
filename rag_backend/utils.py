import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
          "Juli", "August", "September", "Oktober", "November", "Dezember"]

# 1 Token ~ 2.5 Zeichen (deutscher Text). Einheitliche Schaetzung fuer
# Kontext-Budget und Kompaktierungs-Schwelle.
CHARS_PER_TOKEN = 2.5


def get_datetime_berlin() -> str:
    # Feste Zeitzone Europe/Berlin: der Container laeuft in UTC.
    try:
        now = datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        now = datetime.now().astimezone()
    weekday = WEEKDAYS[now.weekday()]
    month = MONTHS[now.month - 1]
    return f"{weekday}, der {now.day}. {month} {now.year}, {now.strftime('%H:%M')} Uhr"


# ---- Gemeinsame LLM-Helper (genutzt von agent.py, memory.py, missions.py,
#      redactor.py).

def strip_think(text) -> str:
    # Entfernt <think>-Bloecke aus Qwen-Antworten, auch bei abgeschnittenem
    # Block oder haengendem Closer ohne Opener.
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    out = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.DOTALL)
    if "</think>" in out:
        out = out.rsplit("</think>", 1)[-1]
    return out.strip()


def extract_json(text, kind: str = "object"):
    # Balance-Scanner statt greedy Regex: liefert den ersten vollstaendig
    # parsebaren JSON-Block. None wenn keiner da ist -- den Fallback
    # entscheidet der Aufrufer.
    if not isinstance(text, str) or not text:
        return None
    open_ch, close_ch = ("[", "]") if kind == "array" else ("{", "}")
    start = text.find(open_ch)
    while start != -1:
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find(open_ch, start + 1)
    return None


def get_env_first(*names: str, default: str = "") -> str:
    """Gibt den Wert der ersten gesetzten und nicht-leeren Umgebungsvariable zurueck."""
    import os
    for name in names:
        val = os.getenv(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return default



def _env_float(name: str, default: float) -> float:
    import os
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def make_qwen_llm(*, temperature: float, top_p: float, max_tokens: int,
                  thinking: bool = False, presence_penalty: float = 0.0,
                  min_p: float | None = None, api_key: str | None = None,
                  tunable: bool = False):
    # Fabrik fuer alle lokalen LLM-Instanzen. Imports lokal, damit
    # utils ohne langchain importierbar bleibt.
    # Die Vorgaben sind auf Qwen abgestimmt. Ein anderes Modell braucht oft andere
    # Werte (Gemma 4: 1.0 / 0.95 / 64): LLM_TOP_K gilt fuer alle Instanzen,
    # LLM_TEMPERATURE und LLM_TOP_P nur fuer die mit tunable=True (die Antworten),
    # nicht fuer kurze Pruefungen, die bewusst fast deterministisch laufen.
    import os
    from langchain_openai import ChatOpenAI
    if tunable:
        temperature = _env_float("LLM_TEMPERATURE", temperature)
        top_p = _env_float("LLM_TOP_P", top_p)
    top_k = int(_env_float("LLM_TOP_K", 20))
    extra_body = {"chat_template_kwargs": {"enable_thinking": thinking}, "top_k": top_k}
    if min_p is not None:
        extra_body["min_p"] = min_p
    effective_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("SGLANG_API_KEY") or "dummy"
    return ChatOpenAI(
        model=os.getenv("OAI_MODEL") or "default-model",
        base_url=os.getenv("OAI_BASE_URL", "http://llm:8080/v1"),
        api_key=effective_key,
        temperature=temperature,
        top_p=top_p,
        presence_penalty=presence_penalty,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
