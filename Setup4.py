#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Setup4.py -- Cloud-Agenten: Missions-System mit Gemini-Workern und PII-Schutz.

Generiert:
  rag_backend/cloud_agents/__init__.py      (Tool-Registrierung)
  rag_backend/cloud_agents/providers.py     (Gemini-Adapter + Rate-Limiter)
  rag_backend/cloud_agents/redactor.py      (Pseudonymisierung + PII-Tripwire)
  rag_backend/cloud_agents/missions.py      (Missions-Engine: Planner/Worker/Reviewer-Loop)
  rag_backend/action_engine/tools/cloud_missions.py  (BaseTool-Tools fuer Qwen)
  alembic/versions/0007_missions.py         (missions-Tabelle)
  rag_backend/tests/test_cloud_agents.py    (Offline-Unit-Tests)
  API_Tokens/Gemini.txt                     (nur falls fehlend: leerer Platzhalter)
  .env SETUP4-Block                          (CLOUD_* ConfigVars)

PII-Erkennung ist generisch (Regex-Muster + Qwen-NER, fail-closed) -- KEINE
gepflegte Liste noetig. Optional kann <Workspace>/identity/pii_blocklist.txt
manuell angelegt werden (eine Zeile = ein Garantie-Begriff); wird sie gefunden,
prueft der Tripwire sie zusaetzlich. Setup4 erzeugt diese Datei NICHT.

Reihenfolge: Setup1 -> Setup2 -> Setup3 -> Setup4 -> docker compose up -d --build
Setup1-3 muessen vorher gelaufen sein (setup_common.py, rag_backend/, action_engine/).
"""

import os
import stat
from pathlib import Path

try:
    from setup_common import _log, writefile, write_env_block, prune_env_keys
except ImportError:
    raise SystemExit("setup_common.py fehlt -- bitte zuerst 'python Setup1.py' ausfuehren.")

try:
    from setup_common import set_permissions
except ImportError:
    set_permissions = None

try:
    from setup_common import verify_markers
except ImportError:
    verify_markers = None

BASE_DIR = Path(__file__).parent.resolve()
API_DIR = BASE_DIR / "API_Tokens"
WORKSPACE_DIR = Path(os.getenv("ARGUS_WORKSPACE_DIR", "C:/Argus_Workspace"))


# ===========================================================================
# rag_backend/cloud_agents/__init__.py
# ===========================================================================
CLOUD_INIT_PY = r"""
'''Cloud-Agenten (Setup4): Delegation an Gemini mit PII-Schutz.

Das lokale Qwen bleibt Orchestrator und einzige Instanz mit Rohdaten-Zugriff.
Alles, was den Host verlaesst, laeuft durch redactor.py (Pseudonymisierung +
Tripwire). Missionen arbeiten im Hintergrund-Loop (missions.mission_loop).
'''


def register_cloud_tools() -> list:
    # Liefert die LangChain-Tools der Cloud-Agenten.
    from rag_backend.action_engine.tools.cloud_missions import (
        StartMissionTool,
        MissionStatusTool,
        CancelMissionTool,
        CloudAskTool,
    )
    return [
        StartMissionTool().to_langchain_tool(),
        MissionStatusTool().to_langchain_tool(),
        CancelMissionTool().to_langchain_tool(),
        CloudAskTool().to_langchain_tool(),
    ]
"""


# ===========================================================================
# rag_backend/cloud_agents/providers.py
# ===========================================================================
PROVIDERS_PY = r"""
'''Provider-Adapter fuer Cloud-LLMs (Setup4).

- GeminiProvider: google-genai SDK, Free-Tier-tauglich (RPM/RPD-Limiter).

SDK-Importe passieren lazy in den Methoden, damit dieses Modul auch ohne
installierte SDKs importierbar bleibt (Tests, Tooling). Rate-Limits sind
rollierende Fenster (60 s / 24 h) -- konservativer als Googles Tageslimit
mit Reset um Mitternacht Pacific Time.
'''
import asyncio
import logging
import os
import time
from collections import deque

log = logging.getLogger(__name__)


class CloudNotConfigured(RuntimeError):
    '''Kein Provider hat einen API-Key (API_Tokens/Gemini.txt).'''


class CloudRateLimited(RuntimeError):
    '''Rate-/Tageslimit erreicht; retry_after in Sekunden als Hinweis.'''

    def __init__(self, msg: str, retry_after: float = 60.0):
        super().__init__(msg)
        self.retry_after = retry_after


class CloudCallFailed(RuntimeError):
    '''Cloud-Call endgueltig fehlgeschlagen (API-Fehler, leere Antwort, Timeout).'''


class RollingLimiter:
    '''Rollierendes RPM/RPD-Fenster. rpd=0 bedeutet: kein Tageslimit.'''

    def __init__(self, rpm: int, rpd: int):
        self.rpm = max(0, rpm)
        self.rpd = max(0, rpd)
        self._minute: deque = deque()
        self._day: deque = deque()

    def _prune(self, now: float) -> None:
        while self._minute and now - self._minute[0] > 60.0:
            self._minute.popleft()
        while self._day and now - self._day[0] > 86400.0:
            self._day.popleft()

    def seconds_until_slot(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune(now)
        if self.rpd and len(self._day) >= self.rpd:
            return 86400.0 - (now - self._day[0])
        if self.rpm and len(self._minute) >= self.rpm:
            return 60.0 - (now - self._minute[0])
        return 0.0

    def record(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._minute.append(now)
        self._day.append(now)


# rpm, rpd -- Free-Tier-Quota je Modell. Unbekannte Modelle fallen auf
# (default_rpm, default_rpd) des Providers zurueck. Modell-IDs muessen exakt
# den API-Namen entsprechen.
_MODEL_LIMITS = {
    # 3.7: Google nennt die Free-Tier-Quote nicht mehr in der Doku (Verweis auf
    # AI Studio). Bis zur Gegenprobe wie die uebrigen vollen Flash angesetzt --
    # zu niedrig geraten bremst nur, zu hoch geraten holt 429er.
    "gemini-3.7-flash":       (5, 20),
    "gemini-3.6-flash":       (5, 20),
    "gemini-3.5-flash":       (5, 20),
    "gemini-3-flash-preview": (5, 20),
    "gemini-3.5-flash-lite":  (15, 500),
    "gemini-3.1-flash-lite":  (15, 500),
    "gemini-2.5-flash":       (5, 20),
    "gemini-2.5-flash-lite":  (10, 20),
    "gemma-4-31b-it":         (30, 14400),
    "gemma-4-26b-a4b-it":     (30, 14400),
}

#--- TPM (Token pro Minute) je Modell -- die dritte Quote neben RPM/RPD und die
#--- einzige, die vom Payload abhaengt. Nicht im Limiter erzwungen (der zaehlt
#--- Requests); dient der Auslegung von CLOUD_PAYLOAD_MAX_CHARS.
_MODEL_TPM = {
    "gemini-3.7-flash":       250_000,
    "gemini-3.6-flash":       250_000,
    "gemini-3.5-flash":       250_000,
    "gemini-3-flash-preview": 250_000,
    "gemini-3.5-flash-lite":  250_000,
    "gemini-3.1-flash-lite":  250_000,
    "gemma-4-31b-it":          16_000,
    "gemma-4-26b-a4b-it":      16_000,
}


class CloudProvider:
    '''Gemeinsames Interface: generate() kapselt Limiter, Timeout und Fehler-Mapping.'''

    name = "base"
    key_var = ""

    def __init__(self, rpm: int, rpd: int):
        self.default_rpm = rpm
        self.default_rpd = rpd
        self.model = "unbekannt"
        self._limiters = {}  # (key_idx, model) -> RollingLimiter
        self._current_client_idx = 0

    def _api_key(self) -> str:
        from rag_backend.crypto_utils import load_secret
        try:
            return (load_secret(self.key_var) or "").strip()
        except Exception as e:
            log.warning(f"Secret {self.key_var} nicht lesbar: {e}")
            return ""

    def _api_keys(self) -> list[str]:
        raw = self._api_key()
        if not raw:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def is_configured(self) -> bool:
        return bool(self._api_keys())

    def _get_limiter(self, key_idx: int, model: str) -> RollingLimiter:
        key = (key_idx, model)
        if key not in self._limiters:
            rpm, rpd = _MODEL_LIMITS.get(model, (self.default_rpm, self.default_rpd))
            self._limiters[key] = RollingLimiter(rpm=rpm, rpd=rpd)
        return self._limiters[key]

    async def generate(self, system: str, prompt: str, max_tokens: int,
                       grounding: bool = False, model: str | None = None) -> tuple[str, int, int]:
        '''Liefert (text, tokens_in, tokens_out). Wirft CloudRateLimited/CloudCallFailed.

        grounding=True aktiviert providerseitige Live-Websuche (nur Gemini; sonst
        ignoriert) -- gegen veraltetes Trainingswissen. model ueberschreibt pro Call
        das Standard-Modell (Pro-Rollen-Modellwahl: z.B. staerkeres Modell zum Planen,
        RPD-starkes zum Arbeiten). Jeder Call = 1 Request im RPM/RPD-Fenster.'''
        mdl = model or self.model
        keys = self._api_keys()
        if not keys:
            raise CloudNotConfigured(f"{self.name}: Keine API-Keys in {self.key_var} vorhanden.")

        num_keys = len(keys)
        timeout = float(os.getenv("CLOUD_CALL_TIMEOUT_SECONDS", "120"))
        last_error = None

        for attempt in range(num_keys):
            idx = (self._current_client_idx + attempt) % num_keys
            limiter = self._get_limiter(idx, mdl)
            
            wait = limiter.seconds_until_slot()
            if wait > 90.0:
                last_error = CloudRateLimited(f"{self.name} Key {idx}: RPD Limit.", wait)
                continue
            if wait > 0.0:
                log.info(f"{self.name}: Key {idx} hat RPM-Limit, warte {wait:.1f}s.")
                await asyncio.sleep(min(wait + 0.5, 65.0))

            self._current_client_idx = idx
            limiter.record()
            
            try:
                text, tokens_in, tokens_out = await asyncio.wait_for(
                    self._generate(system, prompt, max_tokens, grounding=grounding, model=mdl),
                    timeout=timeout,
                )
                return text, tokens_in, tokens_out
            except (CloudRateLimited, Exception) as e:
                code = getattr(e, "code", None)
                log.warning(f"{self.name}: Fehler mit Key {idx} ({keys[idx][:10]}...): {e}")
                
                if isinstance(e, CloudRateLimited) or code in (429, 400, 403) or "429" in str(e):
                    limiter.record(time.monotonic() + 3600.0)
                else:
                    limiter.record(time.monotonic() + 300.0)
                last_error = e
                continue

        raise CloudCallFailed(f"{self.name}: Alle {num_keys} Keys fehlgeschlagen. Letzter Fehler: {last_error}")

    async def _generate(self, system: str, prompt: str, max_tokens: int,
                        grounding: bool = False, model: str | None = None) -> tuple[str, int, int]:
        raise NotImplementedError


class GeminiProvider(CloudProvider):
    name = "gemini"
    key_var = "GEMINI_API_KEY"

    def __init__(self):
        super().__init__(
            rpm=int(os.getenv("CLOUD_GEMINI_RPM", "8")),
            rpd=int(os.getenv("CLOUD_GEMINI_RPD", "200")),
        )
        self.model = os.getenv("CLOUD_GEMINI_MODEL", "gemini-3.1-flash-lite")
        # Nach einem grounded-429 werden grounded-Versuche 6h uebersprungen.
        self._grounding_blocked_until = 0.0
        self._clients = []

    @staticmethod
    def _grounding_sources(resp) -> str:
        '''Haengt die genutzten Web-Quellen an (aus grounding_metadata). Liefert Gemini
        keine Quell-Chunks (bei gemini-2.5-flash oft der Fall, Zitate stehen dann inline),
        hat aber gesucht, wird wenigstens ein Hinweis mit den Suchanfragen angehaengt --
        so ist die Live-Recherche immer sichtbar. Leer, wenn gar nicht gegroundet.'''
        try:
            cands = getattr(resp, "candidates", None) or []
            gm = getattr(cands[0], "grounding_metadata", None) if cands else None
            if gm is None:
                return ""
            chunks = getattr(gm, "grounding_chunks", None) or []
            seen, lines = set(), []
            for ch in chunks:
                web = getattr(ch, "web", None)
                uri = getattr(web, "uri", None)
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                title = (getattr(web, "title", None) or uri).strip()
                lines.append(f"- {title} -- {uri}")
            if lines:
                return "\n\nQuellen:\n" + "\n".join(lines[:10])
            queries = getattr(gm, "web_search_queries", None) or []
            if queries:
                return "\n\n(Live-Recherche via Google -- Suchanfragen: " + "; ".join(queries[:5]) + ")"
            return ""
        except Exception:
            return ""

    async def _generate(self, system: str, prompt: str, max_tokens: int,
                        grounding: bool = False, model: str | None = None) -> tuple[str, int, int]:
        from google import genai
        from google.genai import errors, types

        keys = self._api_keys()
        if not keys:
            raise CloudCallFailed("gemini: Keine API-Keys konfiguriert.")

        if len(self._clients) != len(keys):
            self._clients = [genai.Client(api_key=k) for k in keys]
            self._current_client_idx = min(self._current_client_idx, len(self._clients) - 1)

        client = self._clients[self._current_client_idx]
        mdl = model or self.model
        if mdl.startswith("gemma"):
            if system:
                prompt = system + "\n\n" + prompt
            system = None
            grounding = False
        if grounding and time.monotonic() < self._grounding_blocked_until:
            grounding = False
        cfg = dict(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            temperature=0.4,
        )
        if grounding:
            cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        try:
            resp = await client.aio.models.generate_content(
                model=mdl,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg),
            )
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 429 and grounding:
                self._grounding_blocked_until = time.monotonic() + 6 * 3600
                log.warning("gemini: 429 bei grounded Call -- Retry ohne Grounding (Suchfundierungs-Kontingent erschoepft/0; Memo 6h).")
                return await self._generate(system, prompt, max_tokens, grounding=False, model=model)
            if code == 429:
                raise CloudRateLimited(f"gemini: 429 vom API ({e}).", 60.0)
            if code in (400, 403):
                raise CloudRateLimited(f"gemini: Auth-Fehler ({code}): {e}", 3600.0)
            raise CloudCallFailed(f"gemini: APIError {code}: {e}")
        except Exception as e:
            raise CloudCallFailed(f"gemini: {type(e).__name__}: {e}")

        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            try:
                cands = getattr(resp, "candidates", None) or []
                parts = getattr(getattr(cands[0], "content", None), "parts", None) or []
                text = "".join((getattr(pt, "text", "") or "") for pt in parts).strip()
            except Exception:
                text = ""
        if not text:
            raise CloudCallFailed("gemini: leere Antwort (Safety-Block oder kein Kandidat).")
        if grounding:
            text += self._grounding_sources(resp)
        um = getattr(resp, "usage_metadata", None)
        tokens_in = int(getattr(um, "prompt_token_count", 0) or 0)
        tokens_out = int(getattr(um, "candidates_token_count", 0) or 0)
        return text, tokens_in, tokens_out


# Instanzen werden gecacht, damit der Limiter-Zustand ueber Calls hinweg lebt.
# Aktuell nur Gemini; die Abstraktion traegt einen zweiten Anbieter ohne Umbau.
_PROVIDERS: dict = {}


def _instances() -> dict:
    if not _PROVIDERS:
        _PROVIDERS["gemini"] = GeminiProvider()
    return _PROVIDERS


def get_provider(preferred: str | None = None) -> CloudProvider:
    '''Bevorzugten, konfigurierten Provider liefern; sonst irgendeinen; sonst Fehler.'''
    inst = _instances()
    default = os.getenv("CLOUD_PROVIDER_DEFAULT", "gemini").strip().lower()
    first = (preferred or "").strip().lower() or default
    order = [first] + [n for n in inst if n != first]
    for name in order:
        p = inst.get(name)
        if p is not None and p.is_configured():
            return p
    raise CloudNotConfigured(
        "Kein Cloud-Provider konfiguriert -- API-Key in API_Tokens/Gemini.txt "
        "hinterlegen (leer = deaktiviert)."
    )


def get_fallback(current_name: str):
    '''Anderen konfigurierten Provider liefern oder None.

    Mit Gemini als einzigem Provider liefert das immer None -- die Aufrufer in
    missions.py behandeln das seit jeher (kein Fallback = Fehler durchreichen).
    Die Funktion bleibt, damit ein zweiter Anbieter nur eine Zeile in _instances
    kostet und nicht wieder Fallback-Logik nachgezogen werden muss.'''
    inst = _instances()
    for name, p in inst.items():
        if name != current_name and p.is_configured():
            return p
    return None


def any_configured() -> bool:
    return any(p.is_configured() for p in _instances().values())
"""


# ===========================================================================
# rag_backend/cloud_agents/redactor.py
# ===========================================================================
REDACTOR_PY = r"""
'''Redaction-Pipeline (Setup4): nichts verlaesst den Host ohne diese Schichten.

1) redact_deterministic(): musterbasierte Regex (E-Mail, IBAN, Telefon,
   Geburtsdatum) -> stabile Platzhalter wie [EMAIL_1] + Mapping (bleibt lokal,
   Fernet-verschluesselt in DB). Muster statt Listen -- null Pflegeaufwand.
2) redact_llm_ner(): DIE tragende generische Schicht. Qwen extrahiert PII als
   JSON (NER) -- erkennt auch unbekannte Namen/Adressen beliebiger Personen,
   ohne gepflegte Liste. Der Austausch gegen Platzhalter bleibt
   deterministischer Code (kein Halluzinationsrisiko, Re-Hydration moeglich).
   Fail-closed (CLOUD_REDACTION_FAIL_CLOSED, Default true): ohne
   funktionierenden NER-Pass verlaesst nichts den Host.
3) tripwire(): hartes, NICHT ueberredbares Gate vor JEDEM Outbound-Payload
   (Regex E-Mail/IBAN; Prompt-Injection im Inhalt kann eine Regex nicht
   aushebeln -- ein LLM schon). Telefon/Datum hier bewusst NICHT (zu viele
   False-Positives in Recherche-Inhalten); dafuer redigiert Schicht 1.

Optional: existiert identity/pii_blocklist.txt (PII_BLOCKLIST_PATH), werden
diese Begriffe zusaetzlich deterministisch ersetzt und im Tripwire geprueft --
eine Garantie-Liste fuer Einzelfaelle, KEIN Pflicht-Mechanismus. Ohne Datei
laeuft alles vollstaendig ueber Regex + Qwen-NER.

rehydrate() ersetzt Platzhalter lokal zurueck (nach der finalen Synthese).
'''
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b")
_TEL_RE = re.compile(
    r"(?<!\d)(?:\+49|0049)[ \-/]?\d(?:[ \-/]?\d){5,14}(?!\d)"
    r"|(?<![\d.,])0\d{2,4}[ \-/]\d(?:[ \-/]?\d){2,9}(?!\d)"
    r"|(?<![\d.,])0\d{9,11}(?!\d)"
)
# Geburtsdatum nur mit 'geb.'-Kontext redigieren -- freie Datumsangaben bleiben.
_GEB_RE = re.compile(
    r"(?i)(geb(?:\.|oren)?(?:\s*am)?\s*:?\s*)([0-3]?\d\.[01]?\d\.(?:19|20)\d\d)"
)

#--- Zugangsdaten. Erkennung ueber die BENENNUNG (password:, api_key=, token),
#--- nicht ueber die Gestalt des Werts. Stehen zusaetzlich im tripwire(), dort
#--- als harter Abbruch.
#--- Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
_SECRET_ASSIGN_RE = re.compile(
    r'''(?ix)
    \b(
        pass(?:wor[dt])? | pwd | passphrase | kennwort |
        secret | api[_\-\s]?key | apikey | access[_\-\s]?key |
        client[_\-\s]?secret | auth[_\-\s]?token | bearer |
        token | credentials? | conn(?:ection)?[_\-\s]?string
    )
    \s*[:=]\s*
    ["']?
    #--- Ein bereits gesetzter Platzhalter ist kein Fund.
    (?! \[ [A-Z]+ _ \d+ \] )
    ([^\s"',;]{4,})
    ["']?
    '''
)

#--- Bekannte Schluessel-/Token-Formate: PEM-Bloecke, JWTs, Provider-Praefixe.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
    re.DOTALL,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_TOKEN_PREFIX_RE = re.compile(
    r"(?i)\b(?:"
    r"sk-[A-Za-z0-9_-]{16,}|"          # OpenAI-artig
    r"hf_[A-Za-z0-9]{16,}|"            # HuggingFace
    r"gh[pousr]_[A-Za-z0-9]{16,}|"     # GitHub
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"   # Slack
    r"AKIA[0-9A-Z]{16}|"               # AWS Access Key ID
    r"AIza[0-9A-Za-z_-]{20,}"          # Google API Key
    r")\b"
)
#--- SSH-Privatekey-Rumpf ohne PEM-Header (abgeschnitten kopiert o.ae.).
_OPENSSH_KEY_RE = re.compile(r"\bb3BlbnNzaC1rZXktdjE[A-Za-z0-9+/=]*")

_BLOCKLIST_CACHE: dict = {"path": None, "mtime": None, "terms": []}


def _blocklist_path() -> Path:
    return Path(
        os.getenv("PII_BLOCKLIST_PATH", "/host/argus_workspace/identity/pii_blocklist.txt")
    )


def _load_blocklist() -> list:
    '''Optionale Garantie-Begriffe, laengste zuerst. Fehlende Datei ist der
    Normalfall (kein Warning) -- die generische Erkennung macht redact_llm_ner.'''
    path = _blocklist_path()
    try:
        if not path.exists():
            if _BLOCKLIST_CACHE["path"] != str(path):
                log.info(f"Keine optionale PII-Blocklist ({path}) -- Regex + Qwen-NER aktiv.")
                _BLOCKLIST_CACHE.update({"path": str(path), "mtime": None, "terms": []})
            return []
        mtime = path.stat().st_mtime
        if _BLOCKLIST_CACHE["path"] == str(path) and _BLOCKLIST_CACHE["mtime"] == mtime:
            return _BLOCKLIST_CACHE["terms"]
        terms = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            terms.append(term)
        terms.sort(key=len, reverse=True)
        _BLOCKLIST_CACHE.update({"path": str(path), "mtime": mtime, "terms": terms})
        log.info(f"PII-Blocklist geladen: {len(terms)} Begriffe.")
        return terms
    except Exception as e:
        log.warning(f"PII-Blocklist nicht lesbar ({path}): {e}")
        return _BLOCKLIST_CACHE.get("terms") or []


def _next_placeholder(mapping: dict, category: str) -> str:
    n = 1 + sum(1 for k in mapping if k.startswith("[" + category + "_"))
    return "[" + category + "_" + str(n) + "]"


def redact_deterministic(text: str, mapping: dict | None = None):
    '''Pseudonymisiert text; liefert (redigierter_text, mapping).

    mapping ist platzhalter -> originalwert und kann ueber mehrere Aufrufe
    weitergereicht werden (gleiche Werte -> gleiche Platzhalter).
    '''
    mapping = dict(mapping or {})
    reverse = {v: k for k, v in mapping.items()}

    def _placeholder_for(value: str, category: str) -> str:
        ph = reverse.get(value)
        if ph is None:
            ph = _next_placeholder(mapping, category)
            mapping[ph] = value
            reverse[value] = ph
        return ph

    out = text or ""

    for term in _load_blocklist():
        rx = re.compile(re.escape(term), re.IGNORECASE)
        out = rx.sub(lambda m: _placeholder_for(m.group(0), "PII"), out)

    #--- Zugangsdaten zuerst: sonst zerlegt ein anderes Muster den Wert und der
    #--- Tripwire findet ihn danach nicht mehr.
    out = _PRIVATE_KEY_RE.sub(lambda m: _placeholder_for(m.group(0), "SECRET"), out)
    out = _OPENSSH_KEY_RE.sub(lambda m: _placeholder_for(m.group(0), "SECRET"), out)
    out = _JWT_RE.sub(lambda m: _placeholder_for(m.group(0), "SECRET"), out)
    out = _TOKEN_PREFIX_RE.sub(lambda m: _placeholder_for(m.group(0), "SECRET"), out)
    #--- Nur den WERT ersetzen, die Benennung bleibt stehen. Ueber die Match-Spans,
    #--- nicht per str.replace -- bei 'token=token' traefe replace die Benennung.
    def _mask_secret_value(m):
        ph = _placeholder_for(m.group(2), "SECRET")
        start = m.start()
        return m.group(0)[: m.start(2) - start] + ph + m.group(0)[m.end(2) - start:]

    out = _SECRET_ASSIGN_RE.sub(_mask_secret_value, out)

    out = _IBAN_RE.sub(lambda m: _placeholder_for(m.group(0), "IBAN"), out)
    out = _EMAIL_RE.sub(lambda m: _placeholder_for(m.group(0), "EMAIL"), out)
    out = _TEL_RE.sub(lambda m: _placeholder_for(m.group(0), "TEL"), out)
    out = _GEB_RE.sub(lambda m: m.group(1) + _placeholder_for(m.group(2), "DATUM"), out)

    return out, mapping


class RedactionUnavailable(RuntimeError):
    '''Qwen-NER-Pass nicht verfuegbar und CLOUD_REDACTION_FAIL_CLOSED aktiv.'''


# Prompt englisch, das Beispiel deutsch: die Eingabetexte sind deutsch.
_NER_SYSTEM = (
    "You are a precise PII detector (NER) in a data-protection gateway. "
    "The following text is about to be sent to an external cloud AI. "
    "Extract ALL details about PRIVATE individuals: names, home addresses and "
    "places of residence, phone numbers, e-mail addresses, birth dates, "
    "ID/personnel/customer numbers, bank details, and employers in a personal "
    "context. IMPORTANT: ALWAYS extract private individuals -- even and "
    "especially when the task is about exactly that person (the task remains "
    "solvable because role and context stay in the text). When in doubt, "
    "extract. ONLY exempt: public figures in a professional/research context "
    "(e.g. politicians, authors) and placeholders in square brackets that "
    "already exist. "
    "Example -- text: 'Welche Fortbildung passt fuer Anna Muster aus Bonn?' -> "
    '[{"value": "Anna Muster", "category": "PERSON"}, {"value": "Bonn", "category": "ADRESSE"}] '
    "Answer ONLY with a JSON array; each element has the form "
    '{"value": "exact text excerpt", "category": "PERSON|ADRESSE|TEL|EMAIL|DATUM|ID|KONTO|ORG"}. '
    "Nothing found: []"
)

_NER_CATEGORIES = {"PERSON", "ADRESSE", "TEL", "EMAIL", "DATUM", "ID", "KONTO", "ORG"}

_NER_LLM = None


def _get_ner_llm():
    '''Dedizierte Qwen-Instanz fuer die PII-Erkennung: Temperatur 0.1
    stabilisiert JSON-Detektion (gleiches Muster wie llm_task in agent.py --
    _llm_fast mit 0.7 war als Detektor zu sprunghaft). Lazy konstruiert,
    damit das Modul ohne laufendes SGLang importierbar bleibt.'''
    global _NER_LLM
    if _NER_LLM is None:
        from rag_backend.crypto_utils import load_secret
        from rag_backend.utils import make_qwen_llm
        _NER_LLM = make_qwen_llm(
            temperature=0.1, top_p=0.9, max_tokens=1200,
            api_key=load_secret("SGLANG_API_KEY") or None,
        )
    return _NER_LLM


async def _ask_ner(text: str) -> str:
    '''Interner Qwen-Call (Test-Seam). Immer argus_internal getaggt.'''
    from langchain_core.messages import HumanMessage, SystemMessage
    from rag_backend.utils import strip_think
    res = await _get_ner_llm().ainvoke(
        [SystemMessage(content=_NER_SYSTEM), HumanMessage(content=text[:20000])],
        config={"tags": ["argus_internal"]},
    )
    return strip_think(str(res.content))


async def redact_llm_ner(text: str, mapping: dict | None = None):
    '''Tragende generische Schicht: Qwen findet PII (auch voellig unbekannte
    Namen -- kein gepflegtes Verzeichnis noetig), der Austausch gegen
    Platzhalter bleibt deterministischer Code.

    Liefert (redigierter_text, mapping). NER-Funde landen in der Map, d.h.
    die Re-Hydration setzt auch fremde Namen nach der Synthese lokal zurueck.
    Fail-closed per CLOUD_REDACTION_FAIL_CLOSED (Default true): wenn der
    NER-Pass nicht laeuft, wird der Versand blockiert statt ungeprueft
    durchgewunken.
    '''
    mapping = dict(mapping or {})
    if not text or os.getenv("CLOUD_REDACTION_LLM_PASS", "true").lower() != "true":
        return text, mapping
    fail_closed = os.getenv("CLOUD_REDACTION_FAIL_CLOSED", "true").lower() == "true"
    try:
        raw = await _ask_ner(text)
        # Nicht utils.extract_json: 'kein JSON' (leere Fundliste) und 'kaputtes
        # JSON' (fail-closed) muessen unterscheidbar bleiben.
        match = re.search(r"\[.*\]", raw, flags=re.DOTALL)
        items = json.loads(match.group(0)) if match else []
        if not isinstance(items, list):
            raise ValueError("NER-Antwort ist kein JSON-Array")
    except Exception as e:
        if fail_closed:
            raise RedactionUnavailable(
                f"Qwen-NER-Pass nicht verfuegbar ({e}) -- Versand blockiert (fail-closed)."
            )
        log.warning(f"Qwen-NER-Pass uebersprungen (fail-open): {e}")
        return text, mapping

    reverse = {v: k for k, v in mapping.items()}
    out = text
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        # Mini-Werte und Platzhalter-Echos ueberspringen (sonst kaputte Ersetzungen).
        if len(value) < 3 or re.fullmatch(r"\[[A-Z]+_\d+\]", value):
            continue
        category = str(item.get("category", "PII")).strip().upper()
        if category not in _NER_CATEGORIES:
            category = "PII"
        rx = re.compile(re.escape(value), re.IGNORECASE)

        def _repl(m, _cat=category):
            v = m.group(0)
            ph = reverse.get(v)
            if ph is None:
                ph = _next_placeholder(mapping, _cat)
                mapping[ph] = v
                reverse[v] = ph
            return ph

        out = rx.sub(_repl, out)
    return out, mapping


def tripwire(payload: str) -> str | None:
    '''Letzte Verteidigungslinie vor JEDEM Outbound-Call.

    Liefert den Grund (fuer Audit/Fehlermeldung) oder None wenn sauber.
    '''
    if not payload:
        return None
    low = payload.lower()
    for term in _load_blocklist():
        if term.lower() in low:
            return f"Blocklist-Treffer '{term[:2]}***'"
    #--- Zugangsdaten: harte Grenze, keine Abwaegung. Der Grund wird OHNE den
    #--- Trefferwert gemeldet -- er landet in Audit-Log und Fehlertext.
    if _PRIVATE_KEY_RE.search(payload):
        return "Privater Schluessel im Payload"
    if _OPENSSH_KEY_RE.search(payload):
        return "OpenSSH-Privatekey im Payload"
    if _JWT_RE.search(payload):
        return "JWT/Token im Payload"
    if _TOKEN_PREFIX_RE.search(payload):
        return "API-Token im Payload"
    if _SECRET_ASSIGN_RE.search(payload):
        return "Zugangsdaten (Passwort/Key/Token) im Payload"
    if _EMAIL_RE.search(payload):
        return "E-Mail-Adresse im Payload"
    if _IBAN_RE.search(payload):
        return "IBAN im Payload"
    return None


async def redact_outbound(texts: list, mapping: dict | None = None):
    '''Buendelt die Outbound-Kette redact_deterministic -> redact_llm_ner fuer
    beliebig viele Texte -- EINE Quelle fuer agent.py, missions.py, cloud_missions.py.
    Liefert (redigierte_texte, mapping). RedactionUnavailable propagiert (fail-closed-
    Semantik von redact_llm_ner); tripwire() prueft weiterhin der Aufrufer.'''
    mapping = dict(mapping or {})
    staged = []
    for t in texts:
        red, mapping = redact_deterministic(t or "", mapping)
        staged.append(red)
    out = []
    for red in staged:
        if red:
            red, mapping = await redact_llm_ner(red, mapping)
        out.append(red)
    return out, mapping


def rehydrate(text: str, mapping: dict) -> str:
    '''Platzhalter lokal zurueck in Originalwerte uebersetzen.'''
    out = text or ""
    for ph, original in (mapping or {}).items():
        out = out.replace(ph, original)
    return out
"""


# ===========================================================================
# rag_backend/cloud_agents/missions.py
# ===========================================================================
MISSIONS_PY = r"""
'''Missions-Engine (Setup4): Cloud-Agenten arbeiten in Schleifen.

Ablauf pro Mission (Hintergrund-Task in main.py, Muster _reflection_idle_watcher):
  Planner  (lokal, Qwen)   zerlegt das redigierte Ziel in 1-4 Teilaufgaben
  Worker   (Cloud)         bearbeitet Teilaufgaben; vor JEDEM Call: Tripwire
  Reviewer (lokal, Qwen)   ACCEPT oder RETRY mit Feedback (max. Versuche/Budget)
  Synthese (lokal, Qwen)   kombiniert Ergebnisse, Re-Hydration der Platzhalter
Ergebnis Fernet-verschluesselt in der missions-Tabelle, Notify via Telegram.

Alle lokalen LLM-Calls laufen mit tags=[argus_internal], damit sie nie im
User-Stream landen. Alle DB-Zugriffe sind sync (SQLAlchemy) und werden aus
dem Async-Kontext via asyncio.to_thread aufgerufen.
'''
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from rag_backend.cloud_agents import providers as providers_mod
from rag_backend.cloud_agents import redactor
from rag_backend.cloud_agents.providers import (
    CloudCallFailed,
    CloudNotConfigured,
    CloudRateLimited,
)
from rag_backend.utils import extract_json, strip_think

log = logging.getLogger(__name__)

USER_ID_DEFAULT = 0

# Tests koennen hier eine sqlite-sessionmaker injizieren.
_SESSION_FACTORY = None

_ACTIVE_STATUSES = ("queued", "planning", "running")

# Mission-IDs, denen gerade ein Follow-Stream zuschaut. _deliver ueberspringt
# fuer diese IDs Push/Telegram; ein Absturz raeumt via finally aus.
_FOLLOWED: set = set()

_PLANNER_SYSTEM = (
    "You are the mission planner of ARGUS. Decompose the goal into 1 to 4 "
    "self-contained, CONCRETE subtasks for an external analysis/writing agent. "
    "Rules: (1) Each subtask has a clear, verifiable work product. "
    "(2) Sensible order -- later subtasks may build on earlier ones. "
    "(3) No duplicates, no filler tasks; a few good subtasks beat many vague ones. "
    "(4) If a subtask needs FRESH facts from the web (versions, prices, news, "
    "dates, current situation), write a short, precise web search query for it "
    "(2-6 search terms, language matching the topic); otherwise an empty string. "
    "Personal data has already been replaced by placeholders like [PII_1] -- "
    "keep them unchanged. Answer ONLY with a JSON array of objects:\n"
    '[{"task": "subtask...", "search": "web search query"}, '
    '{"task": "subtask without research need...", "search": ""}]'
)
# Worker-Briefe sind modell-intern -> englisch. Der Marker [DATENBASIS FEHLT]
# bleibt woertlich (Code und Synthese pruefen auf genau diesen String).
_WORKER_SYSTEM = (
    "You are an external work agent (research evaluation, analysis, drafting, text). "
    "You receive a subtask, optional context, previous results and "
    "RESEARCH MATERIAL from a local web search (with source URLs).\n"
    "Method: Answer in clear Markdown (meaningful headings, lists/tables where "
    "they truly condense content). Appropriate depth instead of filler -- every "
    "paragraph carries information. No meta commentary, no 'Happy to help...', "
    "deliver the work product directly.\n"
    "Fact discipline: Concrete current values (versions, measurements, prices, news, "
    "dates) ONLY from RESEARCH MATERIAL, context or previous results -- then cite "
    "the source URL in parentheses. If the data basis is missing, write "
    "[DATENBASIS FEHLT: <what is missing>] instead of guessing. NEVER rely on your "
    "potentially outdated own knowledge for current facts. If the material "
    "contradicts your own knowledge, the material wins.\n"
    "If the text contains placeholders in square brackets (e.g. [PERSON_1]), they "
    "stand for removed personal data: keep using exactly these placeholders "
    "unchanged, invent NO new placeholders and do not ask about their content."
)
_REVIEWER_SYSTEM = (
    "You are the quality reviewer of ARGUS. Check the result against the subtask "
    "on four criteria: (1) Task fully covered (all requested parts)? "
    "(2) Usefully structured (no filler, no meta talk)? (3) Concrete current "
    "values (versions, measurements, prices, news, dates) only with a source "
    "citation or from the subtask/context -- unsourced current values count as "
    "invented -> RETRY. (4) Where the data basis is missing, is that honestly "
    "marked instead of guessed? Minor style issues are NOT a reason for RETRY. "
    "Answer EXACTLY in one of the two formats:\n"
    "ACCEPT\n"
    "RETRY: <concrete, short feedback on what is missing or wrong>"
)
# Missions-Ergebnis geht woertlich an den Nutzer -> Deutsch-Anweisung bleibt.
_SYNTH_SYSTEM = (
    "You are the synthesis step of ARGUS. Merge the partial results into ONE "
    "complete, uniformly structured final answer to the mission goal: one title, "
    "a consistent heading hierarchy, redundancies between partial results merged. "
    "The facts, numbers, dates, names and sources given in the partial results are "
    "BINDING -- take them over unchanged and NEVER replace them with your own, "
    "possibly outdated knowledge, even if they seem wrong to you (the partial "
    "results come from current live research; your training data is older). "
    "Collect all source URLs, deduplicated, in a 'Quellen' section at the end; "
    "replace numbered source references like [1] or [3] with the actual URLs from "
    "the partial results -- no bare number references. Keep [DATENBASIS FEHLT] "
    "markers visible. No meta commentary, only the result. Respond in German."
)


# ---------------------------------------------------------------------------
# Seams (in Tests monkeypatchbar)
# ---------------------------------------------------------------------------

def _get_session():
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY()
    from rag_backend.database import SessionLocal
    return SessionLocal()


async def _ask_local(system: str, prompt: str, think: bool = False, max_tokens: int | None = None) -> str:
    '''Interner Call ans lokale Qwen (SGLang) -- immer argus_internal getaggt.'''
    from rag_backend.agent import _llm_fast, _llm_think
    llm = _llm_think if think else _llm_fast
    if max_tokens:
        llm = llm.bind(max_tokens=max_tokens)
    res = await llm.ainvoke(
        [SystemMessage(content=system), HumanMessage(content=prompt)],
        config={"tags": ["argus_internal"]},
    )
    return strip_think(str(res.content))


def _flag(name: str, default: bool = True) -> bool:
    '''Env-Flag als bool (Default true). Zentral, damit Grounding-/Rollen-Schalter
    einheitlich gelesen werden.'''
    return os.getenv(name, "true" if default else "false").lower() == "true"


def _write_audit(**kwargs) -> None:
    try:
        from rag_backend.action_engine.audit import write_audit_log
        write_audit_log(**kwargs)
    except Exception as e:
        log.warning(f"Mission-Audit fehlgeschlagen: {e}")


async def _notify_user(text: str) -> None:
    '''Proaktive Telegram-Nachricht (Fliesstext, siehe Telegram-Konvention).'''
    if os.getenv("CLOUD_MISSION_NOTIFY_TELEGRAM", "true").lower() != "true":
        return
    try:
        from rag_backend.telegram_bot import _allowed_chat_id, _get_application, _md_to_plain
        app = await _get_application()
        chat_id = _allowed_chat_id()
        if app is None or not chat_id:
            return
        await app.bot.send_message(chat_id=chat_id, text=_md_to_plain(text)[:4000])
    except Exception as e:
        log.warning(f"Mission-Notify fehlgeschlagen: {e}")


def _owui_jwt(user_id: str) -> str:
    '''Signiert ein OpenWebUI-Session-Token (HS256, Claim {"id": ...}).
    WICHTIG: OpenWebUI nutzt den ROHEN Inhalt von WEBUI_SECRET_KEY_FILE als Secret --
    auch wenn er Fernet-"ENC:"-verschluesselt ist (OpenWebUI entschluesselt ihn NICHT).
    Deshalb hier den rohen Datei-Inhalt nehmen, NICHT load_secret (das wuerde "ENC:"
    via Fernet entschluesseln -> anderer Key -> 401). Live verifiziert 11.07.: roher
    Key -> /event 200, entschluesselter Key -> 401.'''
    import jwt
    from pathlib import Path
    path = Path(os.getenv("WEBUI_SECRET_KEY_FILE", "/run/secrets/webui_secret_key"))
    secret = path.read_text(encoding="utf-8").strip() if path.exists() else os.getenv("WEBUI_SECRET_KEY", "")
    return jwt.encode({"id": user_id}, secret, algorithm="HS256")


async def _push_owui(chat_id: str, message_id: str | None, user_id: str | None, content: str) -> bool:
    '''Haengt content live an die Ursprungs-Nachricht im OpenWebUI-Chat an
    (POST /api/v1/chats/{chat}/messages/{msg}/event). True bei 2xx. Braucht chat- und
    user-Id (aus den durchgereichten X-OpenWebUI-*-Headern). Die message_id sendet
    OpenWebUI beim Modell-Request NICHT (nur bei Tool-Calls) -> fehlt sie, holen wir
    die aktuellste Nachricht (history.currentId) via Chat-API.'''
    if not (chat_id and user_id):
        return False
    base = os.getenv("OWUI_BASE_URL", "http://open-webui:8080").rstrip("/")
    try:
        import httpx
        headers = {"Authorization": f"Bearer {_owui_jwt(user_id)}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            if not message_id:
                g = await client.get(f"{base}/api/v1/chats/{chat_id}", headers=headers)
                if g.status_code // 100 == 2:
                    hist = ((g.json() or {}).get("chat") or {}).get("history") or {}
                    message_id = hist.get("currentId")
            if not message_id:
                log.warning("OWUI-Push: keine message_id ermittelbar.")
                return False
            r = await client.post(
                f"{base}/api/v1/chats/{chat_id}/messages/{message_id}/event",
                headers=headers,
                json={"type": "message", "data": {"content": content}},
            )
        if r.status_code // 100 == 2:
            log.info(f"OWUI-Push OK: Chat {chat_id}, Message {message_id}.")
            return True
        log.warning(f"OWUI /event HTTP {r.status_code}: {str(r.text)[:200]}")
    except Exception as e:
        log.warning(f"OWUI-Push Fehler: {e}")
    return False


async def _deliver(data: dict, text: str) -> None:
    '''Missions-Meldung "zurueck zur Quelle": WebUI-Start -> Live-Push in den
    Ursprungs-Chat via OpenWebUI-/event-API; sonst (oder bei fehlender chat_id /
    Push-Fehler) -> Telegram. Zweites Netz: auch ein 2xx-Push kann den Nutzer
    verfehlen (OpenWebUI haengt den Text zwar serverseitig an die Chat-DB an, aber
    ein offener Tab rendert das Event nicht zwingend und ueberschreibt beim naechsten
    Speichern die DB mit seinem alten Stand) -- dafuer liefert undelivered_for_chat
    beim naechsten Request des Chats nach.'''
    d = data or {}
    if d.get("id") in _FOLLOWED and d.get("origin") in ("webui", "dashboard"):
        # Follow-Stream aktiv -- Push/Telegram wuerden doppelt zustellen.
        log.info(f"Mission {d.get('id')}: Follower aktiv -- Push/Telegram uebersprungen.")
        return
    if d.get("origin") == "dashboard" and d.get("dashboard_session_id"):
        # Argus-Chat hat keine Push-API: der Verlauf IST die Datenbank. Das
        # Ergebnis als Assistenz-Nachricht in die Ursprungs-Session genuegt.
        if await asyncio.to_thread(_append_dashboard_message,
                                   d.get("dashboard_session_id"), text):
            return
        log.warning("Argus-Chat-Zustellung nicht moeglich -- Fallback auf Telegram.")
    if (d.get("origin") == "webui" and d.get("owui_chat_id")
            and os.getenv("CLOUD_MISSION_OWUI_PUSH", "true").lower() == "true"):
        if await _push_owui(d.get("owui_chat_id"), d.get("owui_message_id"),
                            d.get("owui_user_id"), text):
            return
        log.warning("OWUI-Push nicht moeglich -- Fallback auf Telegram.")
    await _notify_user(text)


def _append_dashboard_message(session_id, text: str) -> bool:
    '''Missions-Ergebnis als Assistenz-Nachricht in eine Argus-Chat-Session schreiben.

    Sync (ueber asyncio.to_thread aufrufen). Liefert False, wenn die Session nicht
    mehr existiert -- dann faellt die Zustellung auf Telegram zurueck, statt das
    Ergebnis stillschweigend zu verlieren.'''
    if not session_id or not text:
        return False
    try:
        from rag_backend.crypto_utils import encrypt_msg
        from rag_backend.models import ChatSession, Message
        with _get_session() as db:
            if db.get(ChatSession, int(session_id)) is None:
                log.warning(f"Argus-Chat-Session {session_id} existiert nicht mehr.")
                return False
            db.add(Message(session_id=int(session_id), sender="assistant",
                           content_encrypted=encrypt_msg(text)))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"Argus-Chat-Zustellung fehlgeschlagen: {e}")
        return False


# ---------------------------------------------------------------------------
# DB-Helfer (sync; via asyncio.to_thread aufrufen)
# ---------------------------------------------------------------------------

def create_mission(user_id: int, goal_redacted: str, context_redacted: str,
                   pii_map: dict, provider: str, origin: str = "telegram",
                   owui_chat_id: str | None = None, owui_message_id: str | None = None,
                   owui_user_id: str | None = None,
                   dashboard_session_id: int | None = None) -> int:
    from rag_backend.crypto_utils import encrypt_msg
    from rag_backend.models import Mission
    with _get_session() as db:
        m = Mission(
            user_id=user_id if user_id is not None else USER_ID_DEFAULT,
            goal_encrypted=encrypt_msg(goal_redacted),
            context_encrypted=encrypt_msg(context_redacted or ""),
            pii_map_encrypted=encrypt_msg(json.dumps(pii_map or {}, ensure_ascii=False)),
            provider=(provider or "gemini"),
            status="queued",
            current_step="wartet auf Missions-Loop",
            origin=origin or "telegram",
            owui_chat_id=owui_chat_id,
            owui_message_id=owui_message_id,
            owui_user_id=owui_user_id,
            dashboard_session_id=dashboard_session_id,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id


async def launch_mission(goal: str, context: str, provider: str = "") -> tuple:
    '''Zentraler Mission-Start fuer agent_node (Setup2) UND StartMissionTool --
    eine Quelle fuer Checks, Redaction (fail-closed), Tripwire, Origin-Ermittlung
    und den Nutzertext. Liefert (ok, meldung); meldung geht woertlich an den Nutzer.'''
    if os.getenv("CLOUD_AGENTS_ENABLED", "true").lower() != "true":
        return False, "Cloud-Agenten sind deaktiviert (CLOUD_AGENTS_ENABLED=false)."
    if not providers_mod.any_configured():
        return False, ("Kein Cloud-Provider konfiguriert -- API-Key in "
                       "API_Tokens/Gemini.txt hinterlegen.")
    try:
        (red_goal, red_ctx), mapping = await redactor.redact_outbound([goal, context or ""])
    except redactor.RedactionUnavailable as e:
        return False, str(e)
    guard = redactor.tripwire(red_goal + "\n" + red_ctx)
    if guard:
        return False, (f"PII-Tripwire hat blockiert ({guard}). Bitte Ziel/Kontext "
                       "ohne persönliche Daten formulieren.")
    # Herkunft aus dem ContextVar -- lazy Import gegen den Zirkularimport.
    try:
        from rag_backend.agent import request_origin_var
        origin_info = request_origin_var.get() or {}
    except Exception:
        origin_info = {}
    channel = origin_info.get("channel", "telegram")
    owui_chat_id = origin_info.get("owui_chat_id")
    dash_sid = origin_info.get("dashboard_session_id")
    mission_id = await asyncio.to_thread(
        create_mission, USER_ID_DEFAULT, red_goal, red_ctx, mapping,
        provider or os.getenv("CLOUD_PROVIDER_DEFAULT", "gemini"),
        channel, owui_chat_id, origin_info.get("owui_message_id"),
        origin_info.get("owui_user_id"), dash_sid,
    )
    # Push nur bei vorliegender Chat-Kennung, sonst Telegram.
    in_chat = (channel == "webui" and owui_chat_id) or (channel == "dashboard" and dash_sid)
    where = "erscheint anschließend hier im Chat" if in_chat else "kommt per Telegram"
    return True, (f"Mission {mission_id} gestartet (Status: queued). Sie läuft im "
                  f"Hintergrund; das Ergebnis {where}. Fortschritt: mission_status.")


def _load(mission_id: int) -> dict | None:
    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None:
            return None
        return {
            "id": m.id,
            "user_id": m.user_id,
            "goal": decrypt_msg(m.goal_encrypted),
            "context": decrypt_msg(m.context_encrypted) if m.context_encrypted else "",
            "pii_map": json.loads(decrypt_msg(m.pii_map_encrypted) or "{}") if m.pii_map_encrypted else {},
            "provider": m.provider,
            "status": m.status,
            "origin": getattr(m, "origin", None) or "telegram",
            "owui_chat_id": getattr(m, "owui_chat_id", None),
            "owui_message_id": getattr(m, "owui_message_id", None),
            "owui_user_id": getattr(m, "owui_user_id", None),
            "dashboard_session_id": getattr(m, "dashboard_session_id", None),
        }


def _update(mission_id: int, **fields) -> None:
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        m.updated_at = datetime.now(timezone.utc)
        db.commit()


def _finish(mission_id: int, status: str, result: str | None = None, error: str | None = None,
            calls_used: int | None = None, tokens_in: int | None = None,
            tokens_out: int | None = None) -> None:
    from rag_backend.crypto_utils import encrypt_msg
    fields = {"status": status, "current_step": status}
    if result is not None:
        fields["result_encrypted"] = encrypt_msg(result)
    if error is not None:
        fields["error"] = error[:2000]
    if calls_used is not None:
        fields["calls_used"] = calls_used
    if tokens_in is not None:
        fields["tokens_in"] = tokens_in
    if tokens_out is not None:
        fields["tokens_out"] = tokens_out
    _update(mission_id, **fields)


def _next_queued() -> int | None:
    from rag_backend.models import Mission
    with _get_session() as db:
        m = (
            db.query(Mission)
            .filter(Mission.status == "queued")
            .order_by(Mission.id.asc())
            .first()
        )
        return m.id if m else None


def _current_status(mission_id: int) -> str | None:
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        return m.status if m else None


def request_cancel(mission_id: int) -> bool:
    '''Setzt eine aktive Mission auf cancelling. False wenn unbekannt/beendet.'''
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None or (m.status not in _ACTIVE_STATUSES and m.status != "cancelling"):
            return False
        if m.status != "cancelling":
            m.status = "cancelling"
            m.updated_at = datetime.now(timezone.utc)
            db.commit()
        return True


def list_missions_brief(limit: int = 8) -> list:
    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Mission
    with _get_session() as db:
        rows = db.query(Mission).order_by(Mission.id.desc()).limit(limit).all()
        out = []
        for m in rows:
            goal = decrypt_msg(m.goal_encrypted)
            out.append({
                "id": m.id,
                "status": m.status,
                "provider": m.provider,
                "step": m.current_step or "",
                "calls": m.calls_used or 0,
                "tokens_in": m.tokens_in or 0,
                "tokens_out": m.tokens_out or 0,
                "goal": (goal[:120] + "...") if len(goal) > 120 else goal,
                "error": m.error or "",
            })
        return out


def get_result_text(mission_id: int) -> str | None:
    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None or not m.result_encrypted:
            return None
        return decrypt_msg(m.result_encrypted)


# Terminal-Status -> Kennwort der Abschluss-Meldung. Die _deliver-Texte in
# _run_mission und diese Woerter MUESSEN synchron bleiben.
_TERMINAL_NEEDLES = {
    "done": ("abgeschlossen",),
    "failed": ("gescheitert", "gestoppt"),
    "cancelled": ("abgebrochen",),
}


def delivery_text(mission_id: int) -> str | None:
    '''Kanonischer Abschluss-Block einer terminalen Mission -- die EINE
    Format-Quelle fuer Follow-Stream (main.py) UND undelivered_for_chat.
    Der Wortlaut "Mission <id> <kennwort>" ist zugleich die Zustell-Needle
    (_TERMINAL_NEEDLES). None, solange die Mission nicht terminal ist.'''
    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Mission
    cap = int(os.getenv("CLOUD_MISSION_RESULT_CHARS", "6000"))
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None or m.status not in _TERMINAL_NEEDLES:
            return None
        if m.status == "done":
            pii_map = json.loads(decrypt_msg(m.pii_map_encrypted) or "{}") if m.pii_map_encrypted else {}
            goal = redactor.rehydrate(decrypt_msg(m.goal_encrypted), pii_map)
            result = decrypt_msg(m.result_encrypted) if m.result_encrypted else ""
            return (
                f"Mission {m.id} abgeschlossen ({m.provider}, {m.calls_used or 0} Cloud-Calls, "
                f"{m.tokens_in or 0}/{m.tokens_out or 0} Tokens rein/raus).\n"
                f"Ziel: {goal[:150]}\n\n{result[:cap]}"
            )
        if m.status == "cancelled":
            return f"Mission {m.id} abgebrochen."
        return f"Mission {m.id} gescheitert: {m.error or 'unbekannter Fehler'}"


def undelivered_for_chat(chat_id: str, haystack: str, limit: int = 3) -> list:
    '''Safety-Net zum /event-Live-Push: Abschluss-Meldungen aller terminalen
    Missionen dieses OpenWebUI-Chats, die noch nicht im Chat-Verlauf stehen
    (haystack = Text aller Nachrichten des eingehenden Requests). Deterministisch,
    kein LLM-Umweg: das Ergebnis kommt woertlich aus der DB, der Chat-Agent kann
    grounded Missions-Fakten so nicht mit Eigenwissen ueberschreiben.
    Selbstheilend: die nachgelieferte Meldung wird Teil des Verlaufs und faellt
    damit beim naechsten Request aus dieser Liste.'''
    if not chat_id or os.getenv("CLOUD_MISSION_BRIDGE", "true").lower() != "true":
        return []
    from rag_backend.models import Mission
    with _get_session() as db:
        rows = (
            db.query(Mission.id, Mission.status)
            .filter(Mission.owui_chat_id == chat_id,
                    Mission.status.in_(tuple(_TERMINAL_NEEDLES)))
            .order_by(Mission.id.desc())
            .limit(20)
            .all()
        )
    blocks = []
    for mid, status in rows:
        if any(f"Mission {mid} {w}" in haystack for w in _TERMINAL_NEEDLES[status]):
            continue
        text = delivery_text(mid)
        if text:
            blocks.append(text)
        if len(blocks) >= limit:
            break
    return list(reversed(blocks))  # aelteste zuerst, wie die Missionen liefen


def active_for_chat(chat_id: str) -> list:
    '''IDs aller aktiven Missionen (inkl. cancelling) dieses OpenWebUI-Chats --
    Grundlage fuer den Follow-Stream in main.py.'''
    if not chat_id:
        return []
    from rag_backend.models import Mission
    with _get_session() as db:
        rows = (
            db.query(Mission.id)
            .filter(Mission.owui_chat_id == chat_id,
                    Mission.status.in_(_ACTIVE_STATUSES + ("cancelling",)))
            .order_by(Mission.id.asc())
            .all()
        )
        return [r[0] for r in rows]


def active_for_dashboard(session_id) -> list:
    '''Gegenstueck zu active_for_chat fuer den Argus-Chat: aktive Missionen dieser
    Session. Der Follow-Stream in main.py haengt daran, ob er nach der Antwort offen
    bleibt und den Fortschritt weiterreicht.'''
    if not session_id:
        return []
    from rag_backend.models import Mission
    with _get_session() as db:
        rows = (
            db.query(Mission.id)
            .filter(Mission.dashboard_session_id == int(session_id),
                    Mission.status.in_(_ACTIVE_STATUSES + ("cancelling",)))
            .order_by(Mission.id.asc())
            .all()
        )
        return [r[0] for r in rows]


def mission_step(mission_id: int) -> tuple:
    '''(status, current_step) einer Mission -- schlanker Poll fuer den Follow-Stream.'''
    from rag_backend.models import Mission
    with _get_session() as db:
        m = db.get(Mission, mission_id)
        if m is None:
            return ("missing", "")
        return (m.status, m.current_step or "")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _audit_call(mission_id: int, provider_name: str, model: str, payload: str,
                output: str, status: str, latency_ms: int, tokens_in: int, tokens_out: int) -> None:
    _write_audit(
        tool="cloud_call",
        tool_class="read",
        input_hash=hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16],
        output_hash=hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()[:16] if output else None,
        status=status,
        latency_ms=latency_ms,
        user_id=USER_ID_DEFAULT,
        details=json.dumps(
            {"mission_id": mission_id, "provider": provider_name, "model": model,
             "tokens_in": tokens_in, "tokens_out": tokens_out},
            ensure_ascii=False,
        ),
    )


def _parse_plan(raw: str, fallback_goal: str) -> list:
    '''Liefert eine Liste von {"task": str, "search": str}.

    Akzeptiert das neue Objekt-Format {task, search} UND (rueckwaerts-kompatibel)
    reine String-Arrays -- der lokale Qwen-Planner-Fallback darf die alte Form
    liefern; search ist dann leer (keine Recherche).'''
    try:
        data = extract_json(raw, kind="array")
        if isinstance(data, list):
            subtasks = []
            for item in data:
                if isinstance(item, dict):
                    task = str(item.get("task", "")).strip()
                    search = str(item.get("search", "")).strip()
                else:
                    task, search = str(item).strip(), ""
                if task:
                    subtasks.append({"task": task, "search": search[:200]})
            if subtasks:
                return subtasks[:4]
        log.warning("Planner-JSON fehlt/nicht parsebar -- fahre mit Gesamtziel fort.")
    except Exception as e:
        log.warning(f"Planner-JSON nicht parsebar ({e}) -- fahre mit Gesamtziel fort.")
    return [{"task": fallback_goal, "search": ""}]


def _parse_review(raw: str) -> tuple:
    head = (raw or "").strip().upper()
    if head.startswith("RETRY"):
        feedback = (raw.split(":", 1)[1] if ":" in raw else "").strip()
        return "retry", feedback or "Result incomplete -- please be more precise."
    # Fail-open: alles ausser einem klaren Veto gilt als akzeptiert.
    return "accept", ""


async def _local_research(query: str) -> str:
    '''SearXNG-Grounding fuer Missionen: nutzt die vorhandene lokale Web-Engine
    (SearXNG -> Quali-Ranking -> Volltexte) aus dem Kern. Ersetzt das (im
    Free-Tier nicht verfuegbare) Gemini-Grounding: Suchanfragen bleiben auf dem
    Host, zur Cloud geht nur kuratiertes oeffentliches Material -- nach Tripwire.
    Fehler sind nie fatal: leerer String, der Worker markiert [DATENBASIS FEHLT].'''
    if not query:
        return ""
    try:
        from rag_backend.agent import _search_and_read
        n = int(os.getenv("CLOUD_RESEARCH_SOURCES", "4"))
        cap = int(os.getenv("CLOUD_RESEARCH_PER_SOURCE_CHARS", "2500"))
        text = await _search_and_read(
            query, n_sources=n, per_source_chars=cap,
            grounding_note=False, injection_guard=True,
        )
        if isinstance(text, str) and not text.startswith("ERROR"):
            return text
        log.warning(f"Missions-Recherche ohne Ergebnis ({str(text)[:120]}).")
    except Exception as e:
        log.warning(f"Missions-Recherche fehlgeschlagen ({e}) -- Worker arbeitet ohne Material.")
    return ""


def _worker_prompt(goal: str, subtask: str, context: str, prior_results: list,
                   feedback: str, research: str = "") -> str:
    max_chars = int(os.getenv("CLOUD_PAYLOAD_MAX_CHARS", "24000"))
    # Heutiges Datum explizit mitgeben: Modelle mit aelterem Trainingsstand
    # werten korrekte aktuelle Daten sonst als unplausibel ab.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [f"TODAY'S DATE: {today}",
             "MISSION GOAL:\n" + goal, "CURRENT SUBTASK:\n" + subtask]
    if context:
        parts.append("CONTEXT:\n" + context)
    if research:
        parts.append("RESEARCH MATERIAL (local web search, with source URLs):\n" + research)
    if prior_results:
        prior = "\n\n".join(
            f"Result of subtask {i}: {r[:2000]}" for i, (s, r) in enumerate(prior_results, 1)
        )
        parts.append("PREVIOUS RESULTS:\n" + prior)
    if feedback:
        parts.append("FEEDBACK ON THE PREVIOUS ATTEMPT (please fix):\n" + feedback)
    payload = "\n\n".join(parts)
    return payload[:max_chars]


def _model_chain(env_name: str, default: str) -> list:
    '''Kommagetrennte Modell-Kette aus der Env -- generisch erweiterbar, keine
    Code-Liste. Reihenfolge = Praeferenz; bei 429/Fehler wandert der Call zum
    naechsten Modell (so werden ALLE Free-Modelle nutzbar, z.B. Gemma 4 mit
    RPD 1500 als Ausweich hinter Flash-Lite mit RPD 500).'''
    raw = os.getenv(env_name, default)
    return [m.strip() for m in raw.split(",") if m.strip()] or [default]


def _planner_models() -> list:
    # Pro-Rollen-Modellwahl: staerkeres Flash zum Planen, RPD-starkes Lite
    # dahinter, lokaler Qwen-Planner als letzte Stufe.
    return _model_chain("CLOUD_MISSION_PLANNER_MODEL", "gemini-3.7-flash,gemini-3.1-flash-lite")


def _worker_models() -> list:
    return _model_chain("CLOUD_WORKER_MODEL", "gemini-3.1-flash-lite,gemma-4-31b-it")


async def _generate_chain(provider, system: str, prompt: str, max_tokens: int,
                          grounding: bool, models: list) -> tuple[str, int, int, str]:
    '''Probiert die Modell-Kette der Reihe nach; 429/Call-Fehler -> naechstes
    Modell. Liefert (text, tokens_in, tokens_out, benutztes_modell) fuer den
    Audit-Trail. Wirft die letzte Exception, wenn alle Modelle scheitern --
    die bestehenden Fehlerpfade (Provider-Fallback, Retry-Sleep) greifen dann.'''
    last_exc = None
    for m in models:
        try:
            text, ti, to = await provider.generate(
                system, prompt, max_tokens, grounding=grounding, model=m
            )
            return text, ti, to, m
        except (CloudRateLimited, CloudCallFailed) as e:
            log.warning(f"Modell {m}: {str(e)[:110]} -- naechstes Modell der Kette.")
            last_exc = e
    raise last_exc if last_exc else CloudCallFailed("Modell-Kette leer.")


async def _ask_planner(provider, goal: str, context: str) -> tuple[str, int, int, str]:
    '''Planung: standardmaessig Gemini (groesseres Modell zerlegt Ziele besser),
    per CLOUD_MISSION_PLANNER=local auf lokales Qwen umstellbar. Ein Cloud-Fehler
    faellt IMMER hart auf den lokalen Planner zurueck -- die Mission darf nie an
    der Planung sterben. Bewusst OHNE Grounding (die Recherche macht die lokale
    Web-Engine pro Teilaufgabe). Rueckgabe (text, tokens_in, tokens_out, modell);
    lokal = 0/0/"local" (zaehlt nicht ins Cloud-Budget).'''
    prompt = f"GOAL:\n{goal}\n\nCONTEXT (redacted):\n{context[:6000]}"
    use_cloud = os.getenv("CLOUD_MISSION_PLANNER", "gemini").lower() == "gemini"
    if use_cloud and not redactor.tripwire(prompt):  # Tripwire vor JEDEM Outbound
        try:
            max_out = int(os.getenv("CLOUD_CALL_MAX_OUTPUT_TOKENS", "2048"))
            return await _generate_chain(
                provider, _PLANNER_SYSTEM, prompt, max_out, False, _planner_models()
            )
        except Exception as e:
            log.warning(f"Cloud-Planner fehlgeschlagen ({e}) -- Fallback auf lokalen Planner.")
    text = await _ask_local(_PLANNER_SYSTEM, prompt, think=True)
    return text, 0, 0, "local"


async def _run_mission(mission_id: int) -> None:
    data = await asyncio.to_thread(_load, mission_id)
    if data is None:
        return
    goal, context, pii_map = data["goal"], data["context"], data["pii_map"]
    guard = redactor.tripwire(goal) or (context and redactor.tripwire(context))
    if guard:
        await asyncio.to_thread(
            _finish, mission_id, "failed", None,
            f"PII-Tripwire hat blockiert: Goal oder Kontext enthält ungefilterte PII ({guard})."
        )
        await _deliver(data, f"Mission {mission_id} gestoppt: Goal oder Kontext enthält ungefilterte PII ({guard}).")
        return

    max_calls = int(os.getenv("CLOUD_MISSION_MAX_CLOUD_CALLS", "12"))
    max_attempts = max(1, int(os.getenv("CLOUD_MISSION_MAX_ITERATIONS", "2")))
    max_out = int(os.getenv("CLOUD_CALL_MAX_OUTPUT_TOKENS", "2048"))

    try:
        provider = providers_mod.get_provider(data["provider"])
    except CloudNotConfigured as e:
        await asyncio.to_thread(_finish, mission_id, "failed", None, str(e))
        return

    log.info(f"Mission {mission_id} startet (Provider {provider.name}/{provider.model}).")
    await asyncio.to_thread(_update, mission_id, status="planning", current_step="Planung")

    async def _cancelled() -> bool:
        status = await asyncio.to_thread(_current_status, mission_id)
        return status == "cancelling"

    results: list = []
    calls = tin = tout = 0
    t0_plan = asyncio.get_running_loop().time()
    plan_raw, p_i, p_o, p_model = "", 0, 0, "local"
    try:
        plan_raw, p_i, p_o, p_model = await _ask_planner(provider, goal, context)
        subtasks = _parse_plan(plan_raw, goal)
    except Exception as e:
        log.warning(f"Mission {mission_id}: Planner-Fehler ({e}) -- fahre mit Gesamtziel fort.")
        subtasks = [{"task": goal, "search": ""}]
    if p_i or p_o:  # Planner lief in der Cloud -> zaehlt ins Call-/Token-Budget + Audit
        calls += 1
        tin += p_i
        tout += p_o
        latency = int((asyncio.get_running_loop().time() - t0_plan) * 1000)
        await asyncio.to_thread(
            _audit_call, mission_id, provider.name, p_model,
            f"[PLANNER] {goal[:200]}", plan_raw, "ok", latency, p_i, p_o,
        )

    for idx, subtask in enumerate(subtasks, 1):
        if await _cancelled():
            await asyncio.to_thread(_finish, mission_id, "cancelled", None, None, calls, tin, tout)
            await _deliver(data,f"Mission {mission_id} abgebrochen.")
            return
        task, squery = subtask["task"], subtask.get("search", "")
        research = ""
        if squery:
            # SearXNG-Grounding: einmal pro Teilaufgabe, VOR den Worker-Versuchen.
            await asyncio.to_thread(
                _update, mission_id, status="running",
                current_step=f"Teilaufgabe {idx}/{len(subtasks)}: lokale Recherche",
            )
            research = await _local_research(squery)
        feedback = ""
        accepted = False
        for attempt in range(1, max_attempts + 1):
            payload = _worker_prompt(goal, task, context, results, feedback, research)
            
            # Worker läuft LOKAL auf Qwen
            await asyncio.to_thread(
                _update, mission_id, status="running",
                current_step=f"Teilaufgabe {idx}/{len(subtasks)}, Versuch {attempt} (lokales Qwen)",
            )
            t0 = asyncio.get_running_loop().time()
            try:
                text = await _ask_local(_WORKER_SYSTEM, payload, think=True)
                wm = "qwen-local"
                latency = int((asyncio.get_running_loop().time() - t0) * 1000)
                await asyncio.to_thread(
                    _audit_call, mission_id, "local", wm,
                    payload, text, "ok", latency, 0, 0,
                )
            except Exception as e:
                log.error(f"Mission {mission_id}: Lokaler Worker-Fehler ({e})")
                feedback = f"The previous local attempt failed: {e}"
                continue

            # Reviewer läuft in der CLOUD auf Gemini
            t0_rev = asyncio.get_running_loop().time()
            reviewer_prompt = f"SUBTASK:\n{task}\n\nRESULT:\n{text[:8000]}"
            
            # Redaction vor dem Cloud-Versand, fail-closed: ohne NER-Pass wird
            # das Review lokal uebersprungen statt doch zu versenden.
            red_rev_prompt = None
            try:
                (red_rev_prompt,), pii_map = await redactor.redact_outbound([reviewer_prompt], pii_map)
            except redactor.RedactionUnavailable as e:
                log.warning(f"Mission {mission_id}: Reviewer-Redaction nicht verfuegbar ({e}) -- Review uebersprungen.")
                await asyncio.to_thread(
                    _audit_call, mission_id, provider.name, "reviewer",
                    "", "", "redaction_unavailable", 0, 0, 0,
                )
                verdict, fb = "accept", ""

            if red_rev_prompt is not None:
                guard = redactor.tripwire(red_rev_prompt)
                if guard:
                    await asyncio.to_thread(
                        _finish, mission_id, "failed", None,
                        f"Reviewer-Tripwire hat blockiert: {guard}", calls, tin, tout,
                    )
                    await asyncio.to_thread(
                        _audit_call, mission_id, provider.name, "reviewer",
                        reviewer_prompt, "", "tripwire_blocked", 0, 0, 0,
                    )
                    await _deliver(data,
                        f"Mission {mission_id} gestoppt: Reviewer-Tripwire hat blockiert ({guard})."
                    )
                    return
                if calls >= max_calls:
                    log.info(f"Mission {mission_id}: Call-Budget fuer Review erschöpft.")
                    verdict, fb = "accept", ""
                else:
                    try:
                        rev_text, r_i, r_o, r_model = await _generate_chain(
                            provider, _REVIEWER_SYSTEM, red_rev_prompt, max_out,
                            _flag("CLOUD_WORKER_GROUNDING", True), _worker_models(),
                        )
                        calls += 1
                        tin += r_i
                        tout += r_o

                        # Rehydrierung des Feedbacks
                        rev_text = redactor.rehydrate(rev_text, pii_map)

                        latency_rev = int((asyncio.get_running_loop().time() - t0_rev) * 1000)
                        await asyncio.to_thread(
                            _audit_call, mission_id, provider.name, r_model,
                            red_rev_prompt, rev_text, "ok", latency_rev, r_i, r_o,
                        )

                        verdict, fb = _parse_review(rev_text)
                    except Exception as e:
                        log.warning(f"Mission {mission_id}: Reviewer-Fehler ({e}) -- akzeptiere Ergebnis.")
                        verdict, fb = "accept", ""

            if verdict == "accept" or attempt == max_attempts:
                results.append((task, text))
                accepted = True
                break
            feedback = fb
        if not accepted and calls >= max_calls:
            log.info(f"Mission {mission_id}: Call-Budget erschoepft ({max_calls}).")
            break

    if not results:
        await asyncio.to_thread(
            _finish, mission_id, "failed", None,
            "Keine verwertbaren Teilergebnisse (Budget/Fehler).", calls, tin, tout,
        )
        await _deliver(data,f"Mission {mission_id} gescheitert: keine verwertbaren Ergebnisse.")
        return

    await asyncio.to_thread(_update, mission_id, current_step="Synthese (lokal)")
    combined = "\n\n".join(
        f"SUBTASK {i}: {s}\nRESULT {i}:\n{r}" for i, (s, r) in enumerate(results, 1)
    )
    try:
        final = await _ask_local(
            _SYNTH_SYSTEM, f"GOAL:\n{goal}\n\n{combined[:24000]}", think=True
        )
    except Exception as e:
        log.warning(f"Mission {mission_id}: Synthese-Fehler ({e}) -- verwende Rohergebnisse.")
        final = combined
    if len(results) < len(subtasks):
        final += f"\n\n(Hinweis: {len(results)}/{len(subtasks)} Teilaufgaben abgeschlossen -- Call-Budget erschöpft.)"

    final = redactor.rehydrate(final, pii_map)
    await asyncio.to_thread(_finish, mission_id, "done", final, None, calls, tin, tout)
    goal_local = redactor.rehydrate(goal, pii_map)
    # Wortlaut ist zugleich Needle von _TERMINAL_NEEDLES -- bei Aenderung
    # dort nachziehen.
    cap = int(os.getenv("CLOUD_MISSION_RESULT_CHARS", "6000"))
    await _deliver(data,
        f"Mission {mission_id} abgeschlossen ({provider.name}, {calls} Cloud-Calls, "
        f"{tin}/{tout} Tokens rein/raus).\n"
        f"Ziel: {goal_local[:150]}\n\n{final[:cap]}"
    )
    log.info(f"Mission {mission_id} fertig: {calls} Calls, {tin}/{tout} Tokens.")


async def mission_loop(app=None) -> None:
    '''Hintergrund-Loop: nimmt queued-Missionen seriell in Arbeit.

    Seriell ist bewusst: schont Free-Tier-Limits und die lokale GPU
    (Planner/Reviewer laufen auf SGLang).
    '''
    poll = max(5, int(os.getenv("CLOUD_MISSION_POLL_SECONDS", "15")))
    log.info(f"Mission-Loop gestartet (Poll alle {poll}s).")
    while True:
        try:
            await asyncio.sleep(poll)
            mission_id = await asyncio.to_thread(_next_queued)
            if mission_id is None:
                continue
            await _run_mission(mission_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Mission-Loop: {e}")


# ---------------------------------------------------------------------------
# Einzel-Call (cloud_ask -- ROADMAP Phase 3 "semantischer Router"-Fall)
# ---------------------------------------------------------------------------

async def run_cloud_ask(question: str, context: str, provider_name: str = "") -> str:
    (red_q, red_ctx), mapping = await redactor.redact_outbound([question, context or ""])
    payload = red_q + ("\n\nCONTEXT:\n" + red_ctx if red_ctx else "")
    guard = redactor.tripwire(payload)
    if guard:
        raise CloudCallFailed(f"PII-Tripwire hat blockiert: {guard}")
    provider = providers_mod.get_provider(provider_name)
    max_out = int(os.getenv("CLOUD_CALL_MAX_OUTPUT_TOKENS", "2048"))
    t0 = asyncio.get_running_loop().time()
    text, i_tok, o_tok, wm = await _generate_chain(
        provider, _WORKER_SYSTEM, payload, max_out,
        _flag("CLOUD_ASK_GROUNDING", True), _worker_models(),
    )
    latency = int((asyncio.get_running_loop().time() - t0) * 1000)
    await asyncio.to_thread(
        _audit_call, 0, provider.name, wm, payload, text, "ok", latency, i_tok, o_tok
    )
    return f"[{provider.name}/{provider.model}] " + redactor.rehydrate(text, mapping)
"""


# ===========================================================================
# rag_backend/action_engine/tools/cloud_missions.py
# ===========================================================================
CLOUD_TOOLS_PY = r"""
'''Cloud-Missions-Tools (Setup4) -- BaseTool-Hooks, KEIN run()-Override.

start_mission ist WRITE-klassifiziert: die Erst-Freigabe laeuft ueber den
bestehenden Confirmation-Flow (Telegram/WebUI); _impact_description zeigt das
deterministisch redigierte Paket als Vorschau. Nach Freigabe laufen zusaetzlich
Qwen-Redaction-Pass und PII-Tripwire vor jedem tatsaechlichen Versand.
'''
import asyncio
import os

from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput


def _auto_approve() -> bool:
    # Default TRUE: eine Mission ist nicht-destruktiv. Wer trotzdem eine
    # Freigabe will, setzt CLOUD_MISSION_AUTO_APPROVE=false.
    return os.getenv("CLOUD_MISSION_AUTO_APPROVE", "true").lower() == "true"


class StartMissionInput(BaseModel):
    goal: str = Field(
        ..., min_length=10, max_length=4000,
        description="Mission goal, e.g. 'Research X and produce a structured analysis with sources'. The mission fetches current facts itself via local web search.",
    )
    context: str = Field(
        default="", max_length=20000,
        description="Task-relevant context (facts, requirements, text excerpts). NO real names, addresses or contact data -- redaction runs additionally.",
    )
    provider: str = Field(
        default="", pattern="^(|gemini)$",
        description="Optional: 'gemini'. Empty = default provider.",
    )


class StartMissionTool(BaseTool):
    name = "start_mission"
    description = (
        "Starts a background mission: cloud agents work in loops (planning -> "
        "local web research -> drafting -> review) on a larger research, "
        "analysis or write-up. The mission fetches current facts itself and "
        "cites sources; only redacted, pseudonymized data leaves the system. "
        "The result is delivered automatically on completion (originating chat "
        "or Telegram); progress via mission_status. Start directly -- parallel "
        "missions are allowed. For questions needing an IMMEDIATE answer in "
        "chat use web_search/deep_research or cloud_ask instead. Phrase "
        "goal/context without personal data (names, addresses, contacts)."
    )
    classification = ToolClassification.WRITE
    timeout_seconds = 180

    def get_input_schema(self) -> type[BaseModel]:
        return StartMissionInput

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        if _auto_approve():
            return False
        return True

    def _impact_description(self, params: BaseModel) -> str:
        from rag_backend.cloud_agents import redactor
        red_goal, mapping = redactor.redact_deterministic(params.goal)
        red_ctx, mapping = redactor.redact_deterministic(params.context or "", mapping)
        preview = red_goal + (("\n" + red_ctx) if red_ctx else "")
        if len(preview) > 900:
            preview = preview[:900] + " [...]"
        return (
            "Cloud-Mission starten -- dieses redigierte Paket wird an eine externe "
            "KI (Gemini) übertragen:\n\n" + preview +
            "\n\n(Regex-Vorschau. Vor dem Versand läuft zusätzlich der Qwen-NER-Pass, "
            "der auch unbekannte Namen erkennt, sowie der PII-Tripwire.)"
        )

    async def _execute(self, params: StartMissionInput) -> ToolOutput:
        # Kompletter Start-Flow lebt zentral in missions.launch_mission.
        from rag_backend.cloud_agents.missions import launch_mission
        ok, msg = await launch_mission(params.goal, params.context, params.provider)
        if not ok:
            return ToolOutput(success=False, error=msg)
        return ToolOutput(success=True, data=msg)


class MissionStatusInput(BaseModel):
    mission_id: int = Field(
        default=0, ge=0,
        description="Mission ID for details including the result; 0 = overview of recent missions.",
    )


class MissionStatusTool(BaseTool):
    name = "mission_status"
    description = (
        "Shows the status of cloud missions: overview (mission_id=0) or details "
        "including the COMPLETE result (mission_id=<ID>). ALWAYS fetch a mission "
        "result with its concrete ID and take it over verbatim -- NEVER "
        "reconstruct it from memory or from the overview."
    )
    classification = ToolClassification.READ
    timeout_seconds = 15

    def get_input_schema(self) -> type[BaseModel]:
        return MissionStatusInput

    async def _execute(self, params: MissionStatusInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import get_result_text, list_missions_brief
        if params.mission_id:
            rows = await asyncio.to_thread(list_missions_brief, 50)
            row = next((r for r in rows if r["id"] == params.mission_id), None)
            if row is None:
                return ToolOutput(success=False, error=f"Mission {params.mission_id} nicht gefunden.")
            lines = [
                f"Mission {row['id']}: {row['status']} ({row['provider']}, {row['calls']} Calls, "
                f"{row['tokens_in']}/{row['tokens_out']} Tokens rein/raus)",
                f"Schritt: {row['step']}",
                f"Ziel: {row['goal']}",
            ]
            if row["error"]:
                lines.append(f"Fehler: {row['error']}")
            result = await asyncio.to_thread(get_result_text, params.mission_id)
            if result:
                lines.append("Ergebnis:\n" + result)
            return ToolOutput(success=True, data="\n".join(lines))
        rows = await asyncio.to_thread(list_missions_brief, 8)
        if not rows:
            return ToolOutput(success=True, data="Keine Missionen vorhanden.")
        lines = [
            f"#{r['id']} [{r['status']}] ({r['provider']}, {r['calls']} Calls, "
            f"{r['tokens_in']}/{r['tokens_out']} Tok) {r['goal']}"
            # Affordanz gegen Halluzination: fertige Missionen zeigen den Weg
            # zum echten Ergebnistext.
            + (f" -> Ergebnis: mission_status({r['id']})" if r["status"] == "done" else "")
            for r in rows
        ]
        return ToolOutput(success=True, data="\n".join(lines))


class CancelMissionInput(BaseModel):
    mission_id: int = Field(..., ge=1, description="ID of the mission to cancel.")


class CancelMissionTool(BaseTool):
    name = "cancel_mission"
    description = "Cancels a running or queued cloud mission."
    classification = ToolClassification.WRITE
    timeout_seconds = 15

    def get_input_schema(self) -> type[BaseModel]:
        return CancelMissionInput

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        # Abbrechen ist gutartig -- keine Telegram-Bestaetigung noetig.
        return False

    async def _execute(self, params: CancelMissionInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import request_cancel
        ok = await asyncio.to_thread(request_cancel, params.mission_id)
        if not ok:
            return ToolOutput(success=False, error=f"Mission {params.mission_id} nicht gefunden oder bereits beendet.")
        return ToolOutput(success=True, data=f"Mission {params.mission_id} wird abgebrochen.")


class CloudAskInput(BaseModel):
    question: str = Field(
        ..., min_length=5, max_length=4000,
        description="Subject-matter question for the cloud AI. NO personal data.",
    )
    context: str = Field(
        default="", max_length=12000,
        description="Optional task-relevant context (redaction runs automatically).",
    )
    provider: str = Field(
        default="", pattern="^(|gemini)$",
        description="Optional: 'gemini'. Empty = default.",
    )


class CloudAskTool(BaseTool):
    name = "cloud_ask"
    description = (
        "Asks ONE question to an external cloud AI (Gemini) -- for heavy "
        "single tasks (reasoning, analysis, text) that overwhelm the local "
        "model. Gemini answers grounded via Google web search with source URLs; "
        "Claude has no web access. Question and context are pseudonymized "
        "beforehand (PII redaction + tripwire). For multi-source research "
        "prefer web_search/deep_research (local); for multi-step jobs use "
        "start_mission."
    )
    classification = ToolClassification.READ
    timeout_seconds = 180

    def get_input_schema(self) -> type[BaseModel]:
        return CloudAskInput

    async def _execute(self, params: CloudAskInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import run_cloud_ask
        from rag_backend.cloud_agents.providers import (
            CloudCallFailed,
            CloudNotConfigured,
            CloudRateLimited,
            any_configured,
        )
        from rag_backend.cloud_agents.redactor import RedactionUnavailable
        if os.getenv("CLOUD_AGENTS_ENABLED", "true").lower() != "true":
            return ToolOutput(success=False, error="Cloud-Agenten sind deaktiviert (CLOUD_AGENTS_ENABLED=false).")
        if not any_configured():
            return ToolOutput(
                success=False,
                error="Kein Cloud-Provider konfiguriert -- API-Key in API_Tokens/Gemini.txt hinterlegen.",
            )
        try:
            answer = await run_cloud_ask(params.question, params.context, params.provider)
            return ToolOutput(success=True, data=answer)
        except (CloudNotConfigured, CloudRateLimited, CloudCallFailed, RedactionUnavailable) as e:
            return ToolOutput(success=False, error=str(e))
"""


# ===========================================================================
# alembic/versions/0007_missions.py
# ===========================================================================
MIGRATION_0007 = r"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'missions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('goal_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('context_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('pii_map_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('provider', sa.String(), nullable=False, server_default='gemini'),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('current_step', sa.String(), nullable=True),
        sa.Column('calls_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_missions_status', 'missions', ['status'])
    op.create_index('ix_missions_created_at', 'missions', ['created_at'])


def downgrade():
    op.drop_index('ix_missions_created_at', table_name='missions')
    op.drop_index('ix_missions_status', table_name='missions')
    op.drop_table('missions')
"""


# ===========================================================================
# alembic/versions/0008_mission_origin.py
# ===========================================================================
MIGRATION_0008 = r"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    # Herkunft der Mission fuer "Ergebnis zurueck zur Quelle".
    op.add_column('missions', sa.Column('origin', sa.String(), nullable=False, server_default='telegram'))
    op.add_column('missions', sa.Column('owui_chat_id', sa.String(), nullable=True))
    op.add_column('missions', sa.Column('owui_message_id', sa.String(), nullable=True))
    op.add_column('missions', sa.Column('owui_user_id', sa.String(), nullable=True))


def downgrade():
    op.drop_column('missions', 'owui_user_id')
    op.drop_column('missions', 'owui_message_id')
    op.drop_column('missions', 'owui_chat_id')
    op.drop_column('missions', 'origin')
"""


# ===========================================================================
# alembic/versions/0009_mission_dashboard_origin.py
# ===========================================================================
MIGRATION_0009 = r"""
'''Argus-Chat als Missions-Herkunft

Revision ID: 0009
Revises: 0008
'''
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    # Gegenstueck zu owui_chat_id fuer den Argus-Chat.
    op.add_column('missions', sa.Column('dashboard_session_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('missions', 'dashboard_session_id')
"""


# ===========================================================================
# rag_backend/tests/test_cloud_agents.py
# ===========================================================================
TEST_CLOUD_PY = r"""
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
    path.write_text("# Kommentar\nMichael Mustermann\nMusterstrasse 12\n", encoding="utf-8")
    monkeypatch.setenv("PII_BLOCKLIST_PATH", str(path))
    redactor._BLOCKLIST_CACHE.update({"path": None, "mtime": None, "terms": []})
    return path


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------

def test_redactor_roundtrip(blocklist):
    text = (
        "Michael Mustermann wohnt in der Musterstrasse 12, erreichbar unter "
        "max@example.com oder 0171 1234567, IBAN DE89370400440532013000, geb. 01.02.1990."
    )
    red, mapping = redactor.redact_deterministic(text)
    assert "Michael Mustermann" not in red
    assert "Musterstrasse 12" not in red
    assert "max@example.com" not in red
    assert "0171 1234567" not in red
    assert "DE89370400440532013000" not in red
    assert "01.02.1990" not in red
    assert "[PII_1]" in red
    assert redactor.rehydrate(red, mapping) == text


def test_redactor_stable_placeholders(blocklist):
    red1, mapping = redactor.redact_deterministic("Michael Mustermann war da.")
    red2, mapping = redactor.redact_deterministic("Nochmal Michael Mustermann.", mapping)
    ph1 = [k for k in mapping if mapping[k] == "Michael Mustermann"]
    assert len(ph1) == 1
    assert ph1[0] in red1 and ph1[0] in red2


def test_tripwire_blocklist(blocklist):
    assert redactor.tripwire("Bericht ueber michael mustermann und KI") is not None
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

    notified = []

    async def fake_notify(text):
        notified.append(text)

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            return '["Recherchiere Grundlagen", "Fasse zusammen"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL: kombiniertes Ergebnis fuer [PII_1]."

    monkeypatch.setattr(missions, "_notify_user", fake_notify)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    goal = "Erstelle einen Bericht fuer Michael Mustermann ueber KI-Trends."
    red_goal, mapping = redactor.redact_deterministic(goal)
    assert "Michael Mustermann" not in red_goal

    mid = missions.create_mission(0, red_goal, "", mapping, "gemini")
    asyncio.run(missions._run_mission(mid))

    data = missions._load(mid)
    assert data["status"] == "done"
    result = missions.get_result_text(mid)
    assert result is not None
    assert "Michael Mustermann" in result  # Re-Hydration hat gegriffen
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
    assert notified and "27/51 Tokens" in notified[-1]

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

    async def fake_notify(text):
        return None

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            return '["Teilaufgabe A", "Teilaufgabe B"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_notify_user", fake_notify)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
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

    async def fake_notify(text):
        return None

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        if "mission planner" in system:
            planner_local["used"] = True
            return '["Nur eine Teilaufgabe"]'
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_notify_user", fake_notify)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
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

    async def fake_notify(text):
        return None

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        local_prompts.append((system, prompt))
        if "quality reviewer" in system:
            return "ACCEPT"
        return "FINAL fuer [PII_1]."

    monkeypatch.setattr(missions, "_local_research", fake_research)
    monkeypatch.setattr(missions, "_notify_user", fake_notify)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
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

    async def fake_notify(text):
        return None

    async def fake_ask_local(system, prompt, think=False, max_tokens=None):
        return '["Teilaufgabe"]'

    monkeypatch.setattr(missions, "_notify_user", fake_notify)
    monkeypatch.setattr(missions, "_ask_local", fake_ask_local)

    # Ungefiltertes Ziel direkt einschleusen -> Tripwire muss den Versand blocken.
    mid = missions.create_mission(0, "Bericht ueber Michael Mustermann", "", {}, "gemini")
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


def test_owui_jwt_uses_raw_key(tmp_path, monkeypatch):
    # OpenWebUI nutzt den ROHEN Key-Datei-Inhalt -- nicht entschluesseln.
    import jwt as _jwt
    from rag_backend.cloud_agents import missions
    keyfile = tmp_path / "webui_secret_key.txt"
    keyfile.write_text("ENC:rawsecret123\n")   # roher Wert inkl. ENC-Praefix + Newline
    monkeypatch.setenv("WEBUI_SECRET_KEY_FILE", str(keyfile))
    tok = missions._owui_jwt("user-1")
    # verifizierbar mit dem gestrippten ROHEN Inhalt (nicht entschluesselt)
    assert _jwt.decode(tok, "ENC:rawsecret123", algorithms=["HS256"])["id"] == "user-1"


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


def test_mission_delivery_webui_push(tmp_path, monkeypatch, blocklist):
    # WebUI-Start -> Ergebnis wird per _push_owui in den Chat gepusht, NICHT zu Telegram.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mdw.db")
    pushed, notified = [], []

    async def fake_push(chat_id, message_id, user_id, content):
        pushed.append((chat_id, message_id, user_id, content))
        return True

    async def fake_notify(text):
        notified.append(text)

    monkeypatch.setattr(missions, "_push_owui", fake_push)
    monkeypatch.setattr(missions, "_notify_user", fake_notify)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", "msg-1", "user-1")
    asyncio.run(missions._run_mission(mid))

    assert missions._load(mid)["status"] == "done"
    assert pushed and pushed[-1][0] == "chat-1" and pushed[-1][1] == "msg-1"
    assert "Mission" in pushed[-1][3]
    assert not notified  # kein Telegram bei WebUI-Herkunft
    engine.dispose()


def test_mission_delivery_webui_fallback_telegram(tmp_path, monkeypatch, blocklist):
    # Scheitert der OWUI-Push, faellt die Zustellung auf Telegram zurueck.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mdf.db")
    notified = []

    async def fake_push(chat_id, message_id, user_id, content):
        return False

    async def fake_notify(text):
        notified.append(text)

    monkeypatch.setattr(missions, "_push_owui", fake_push)
    monkeypatch.setattr(missions, "_notify_user", fake_notify)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", "msg-1", "user-1")
    asyncio.run(missions._run_mission(mid))
    assert notified  # Fallback griff
    engine.dispose()


def test_bridge_undelivered_done(tmp_path, monkeypatch, blocklist):
    # Zustell-Bruecke: fertige Mission ohne Abschluss-Meldung im Verlauf.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mbr1.db")
    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", "msg-1", "user-1")
    missions._finish(mid, "done", "GROUNDED ERGEBNIS mit Quellen.", None, 3, 100, 200)

    blocks = missions.undelivered_for_chat("chat-1", "User: Starte eine Mission ...")
    assert len(blocks) == 1
    assert f"Mission {mid} abgeschlossen" in blocks[0]
    assert "GROUNDED ERGEBNIS mit Quellen." in blocks[0]
    assert "Michael Mustermann" in blocks[0]  # Ziel wurde re-hydriert
    engine.dispose()


def test_bridge_skips_delivered_and_foreign_chat(tmp_path, monkeypatch, blocklist):
    # Keine Doppelzustellung; fremde Chats bekommen nie fremde Missionen.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mbr2.db")
    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", "msg-1", "user-1")
    missions._finish(mid, "done", "ERGEBNIS.", None, 1, 10, 20)

    haystack = f"Verlauf...\nMission {mid} abgeschlossen (gemini, 1 Cloud-Calls, 10/20 Tokens rein/raus).\n..."
    assert missions.undelivered_for_chat("chat-1", haystack) == []
    assert missions.undelivered_for_chat("chat-2", "") == []
    engine.dispose()


def test_bridge_failed_mission(tmp_path, monkeypatch, blocklist):
    # Auch gescheiterte Missionen werden nachgemeldet (Fehlertext statt Ergebnis).
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mbr3.db")
    red_goal, mapping = redactor.redact_deterministic("Analyse aktueller CVE-Lage.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", None, "user-1")
    missions._finish(mid, "failed", None, "Rate-Limit erschoepft.", 2, 5, 0)

    blocks = missions.undelivered_for_chat("chat-1", "")
    assert len(blocks) == 1
    assert f"Mission {mid} gescheitert" in blocks[0]
    assert "Rate-Limit erschoepft." in blocks[0]
    engine.dispose()


def test_deliver_skips_push_when_followed(tmp_path, monkeypatch, blocklist):
    # Aktiver Follow-Stream -> _deliver ueberspringt Push und Telegram.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl1.db")
    pushed, notified = [], []

    async def fake_push(chat_id, message_id, user_id, content):
        pushed.append(content)
        return True

    async def fake_notify(text):
        notified.append(text)

    monkeypatch.setattr(missions, "_push_owui", fake_push)
    monkeypatch.setattr(missions, "_notify_user", fake_notify)

    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    mid = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                  "webui", "chat-1", "msg-1", "user-1")
    missions._FOLLOWED.add(mid)
    try:
        asyncio.run(missions._run_mission(mid))
    finally:
        missions._FOLLOWED.discard(mid)
    assert missions._load(mid)["status"] == "done"
    assert not pushed and not notified
    engine.dispose()


def test_active_for_chat_and_step(tmp_path, monkeypatch, blocklist):
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl2.db")
    red_goal, mapping = redactor.redact_deterministic("Analyse A.")
    mid1 = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                   "webui", "chat-1", None, "user-1")
    mid2 = missions.create_mission(0, red_goal, "", mapping, "gemini",
                                   "webui", "chat-2", None, "user-1")
    assert missions.active_for_chat("chat-1") == [mid1]  # queued zaehlt als aktiv
    assert missions.mission_step(mid1)[0] == "queued"
    assert missions.mission_step(999999) == ("missing", "")
    missions._finish(mid1, "done", "FERTIG.", None, 1, 1, 1)
    assert missions.active_for_chat("chat-1") == []      # terminal faellt raus
    assert missions.active_for_chat("chat-2") == [mid2]
    assert missions.active_for_chat("") == []
    engine.dispose()


def test_delivery_text_formats(tmp_path, monkeypatch, blocklist):
    # delivery_text ist die EINE Format-Quelle -- die Needle-Woerter muessen
    # exakt im Text stehen.
    engine, missions = _mission_delivery_env(tmp_path, monkeypatch, "mfl3.db")
    red_goal, mapping = redactor.redact_deterministic("Bericht fuer Michael Mustermann.")
    m_done = missions.create_mission(0, red_goal, "", mapping, "gemini", "webui", "chat-1", None, "u")
    m_fail = missions.create_mission(0, red_goal, "", mapping, "gemini", "webui", "chat-1", None, "u")
    m_canc = missions.create_mission(0, red_goal, "", mapping, "gemini", "webui", "chat-1", None, "u")
    m_open = missions.create_mission(0, red_goal, "", mapping, "gemini", "webui", "chat-1", None, "u")
    missions._finish(m_done, "done", "ERGEBNIS X.", None, 1, 1, 1)
    missions._finish(m_fail, "failed", None, "Kaputt.")
    missions._finish(m_canc, "cancelled")

    t_done = missions.delivery_text(m_done)
    assert f"Mission {m_done} abgeschlossen" in t_done
    assert "ERGEBNIS X." in t_done
    assert "Michael Mustermann" in t_done  # Ziel wurde re-hydriert
    assert f"Mission {m_fail} gescheitert" in missions.delivery_text(m_fail)
    assert "Kaputt." in missions.delivery_text(m_fail)
    assert f"Mission {m_canc} abgebrochen" in missions.delivery_text(m_canc)
    assert missions.delivery_text(m_open) is None   # nicht terminal
    assert missions.delivery_text(999999) is None   # unbekannt
    engine.dispose()
"""


# ===========================================================================
# Generator-Logik
# ===========================================================================

def _ensure_token_file(path: Path, label: str) -> None:
    """Legt eine LEERE Platzhalter-Datei an, falls fehlend (leer = Provider aus).

    Vorhandene Dateien (echte Keys!) werden NIEMALS angefasst.
    """
    if path.exists():
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        state = "Key vorhanden" if content else "leer -> Provider deaktiviert"
        _log(f"API-Token {label}: {path.name} ({state})")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        _log(f"API-Token {label}: {path.name} als leeren Platzhalter angelegt (Provider deaktiviert, Compose-Mount funktioniert).")
    if set_permissions is not None:
        try:
            # mode-Argument ist Pflicht, sonst fehlt Gemini.txt die Haertung.
            set_permissions(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as e:
            _log(f"WARNUNG: Permissions fuer {path.name} nicht gesetzt: {e}")


def main() -> None:
    # Setup4 schreibt mit relativen Pfaden, .env und API_Tokens/ ueber BASE_DIR.
    # Ohne chdir landet beides in getrennten Baeumen.
    os.chdir(BASE_DIR)

    _log("--- Starte Setup4: Cloud-Agenten (Gemini, Missionen, PII-Schutz) ---")

    _ensure_token_file(API_DIR / "Gemini.txt", "Gemini")

    #--- Alte CLOUD_CLAUDE_*-Eintraege ausserhalb des SETUP4-Blocks raeumen.
    if prune_env_keys(
        {"CLOUD_CLAUDE_MODEL", "CLOUD_CLAUDE_RPM", "CLOUD_CLAUDE_RPD", "ANTHROPIC_API_KEY"},
        BASE_DIR / ".env",
    ):
        _log("Migration: alte CLOUD_CLAUDE_*-Eintraege aus .env entfernt.")
    _legacy_claude = API_DIR / "Claude.txt"
    if _legacy_claude.exists() and not _legacy_claude.read_text(encoding="utf-8").strip():
        try:
            _legacy_claude.unlink()
            _log("Migration: leere API_Tokens/Claude.txt entfernt (Provider abgeschafft).")
        except OSError as e:
            _log(f"WARNUNG: Claude.txt nicht loeschbar: {e}")
    elif _legacy_claude.exists():
        _log("HINWEIS: API_Tokens/Claude.txt enthaelt noch einen Key, wird aber nicht "
             "mehr verwendet -- manuell loeschen, wenn er nicht anderweitig gebraucht wird.")

    writefile("rag_backend/cloud_agents/__init__.py", CLOUD_INIT_PY, "cloud_agents/__init__.py", do_dedent=False)
    writefile("rag_backend/cloud_agents/providers.py", PROVIDERS_PY, "cloud_agents/providers.py", do_dedent=False)
    writefile("rag_backend/cloud_agents/redactor.py", REDACTOR_PY, "cloud_agents/redactor.py", do_dedent=False)
    writefile("rag_backend/cloud_agents/missions.py", MISSIONS_PY, "cloud_agents/missions.py", do_dedent=False)
    writefile("rag_backend/action_engine/tools/cloud_missions.py", CLOUD_TOOLS_PY, "tools/cloud_missions.py", do_dedent=False)
    writefile("alembic/versions/0007_missions.py", MIGRATION_0007, "alembic 0007_missions", do_dedent=False)
    writefile("alembic/versions/0008_mission_origin.py", MIGRATION_0008, "alembic 0008_mission_origin", do_dedent=False)
    writefile("alembic/versions/0009_mission_dashboard_origin.py", MIGRATION_0009,
              "alembic 0009_mission_dashboard_origin", do_dedent=False)
    writefile("rag_backend/tests/test_cloud_agents.py", TEST_CLOUD_PY, "tests/test_cloud_agents.py", do_dedent=False)

    write_env_block("SETUP4", {
        "CLOUD_AGENTS_ENABLED": "true",
        "CLOUD_PROVIDER_DEFAULT": "gemini",
        # Basis/Fallback fuer cloud_ask: bestes Textmodell zuerst.
        "CLOUD_GEMINI_MODEL": "gemini-3.7-flash",
        # Modell-KETTEN (kommagetrennt): bei 429/Fehler wandert der Call zum
        # naechsten Modell. Planner = Qualitaet (1 Call/Mission), Worker = viele
        # Calls mit grossem Paket, deshalb TPM-starke Modelle zuerst.
        "CLOUD_MISSION_PLANNER_MODEL": "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite",
        "CLOUD_WORKER_MODEL": "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-31b-it,gemma-4-26b-a4b-it",
        # SearXNG-Grounding der Missionen: lokal viel Material sammeln und einem
        # Modell mit grossem Fenster geben.
        "CLOUD_RESEARCH_SOURCES": "8",
        "CLOUD_RESEARCH_PER_SOURCE_CHARS": "4000",
        "CLOUD_GEMINI_RPM": "8",
        "CLOUD_GEMINI_RPD": "200",
        "CLOUD_MISSION_AUTO_APPROVE": "true",   # Missionen sind nicht-destruktiv -> keine Freigabe (destruktive Action-Tools behalten ihre)
        "CLOUD_MISSION_MAX_CLOUD_CALLS": "12",
        "CLOUD_MISSION_MAX_ITERATIONS": "2",
        # Hybrid-Rollen & Grounding (Gemini plant + recherchiert, Qwen prueft/synthetisiert):
        "CLOUD_MISSION_PLANNER": "local",   # 'gemini' = groesseres Modell plant; 'local' = Qwen
        "CLOUD_WORKER_GROUNDING": "true",    # Missions-Worker darf live googeln (Quellen)
        "CLOUD_ASK_GROUNDING": "true",       # cloud_ask liefert nie veraltetes Wissen
        "CLOUD_CALL_MAX_OUTPUT_TOKENS": "4096",
        "CLOUD_CALL_TIMEOUT_SECONDS": "120",
        # Ausgelegt am KLEINSTEN TPM der Worker-Kette: ~10k Token Eingabe plus
        # 4k Ausgabe, damit auch Gemma (16k TPM) als Reserve nutzbar bleibt.
        "CLOUD_PAYLOAD_MAX_CHARS": "40000",
        "CLOUD_MISSION_POLL_SECONDS": "15",
        "CLOUD_REDACTION_LLM_PASS": "true",
        "CLOUD_REDACTION_FAIL_CLOSED": "true",
        "CLOUD_MISSION_NOTIFY_TELEGRAM": "true",
        # Missions-Ergebnis zurueck zur Quelle: WebUI-Start -> OWUI-Chat,
        # Telegram-Start -> Telegram.
        "OWUI_BASE_URL": "http://open-webui:8080",
        "CLOUD_MISSION_OWUI_PUSH": "true",
        "PII_BLOCKLIST_PATH": "/host/argus_workspace/identity/pii_blocklist.txt",
        
        # --- Qwen-Gemini Collaboration Config (Phase 6) ---
        "CLOUD_COLLAB": "true",
        "CLOUD_COLLAB_MAX_DAILY": "150",
        "CLOUD_COLLAB_ARCHITECT_MODEL": "gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite",
        "CLOUD_COLLAB_RESEARCH_MODEL": "gemini-3.1-flash-lite,gemma-4-31b-it,gemma-4-26b-a4b-it",
        "CLOUD_PLAN_MAX_ROUNDS": "3",
        "CODING_SELF_FIX_ATTEMPTS": "3",
        # MUSS groesser als CODING_SELF_FIX_ATTEMPTS sein, sonst ist der
        # cloud_debug-Zweig unerreichbar.
        "CODING_MAX_ITERATIONS": "8",
    }, BASE_DIR / ".env")

    if verify_markers:
        verify_markers(BASE_DIR)

    _log("")
    _log("SETUP4 ABGESCHLOSSEN.")
    _log("Naechste Schritte:")
    _log("  1. API-Key pruefen: API_Tokens/Gemini.txt (vorhanden?)")
    #--- Voller Build, nicht nur rag-backend: die gemeinsame .env haengt an
    #--- mehreren Diensten. Unveraenderte kommen aus dem Layer-Cache.
    _log("  2. docker compose up -d --build")
    _log("PII-Erkennung: Regex-Muster + Qwen-NER, fail-closed -- keine Pflege-Liste noetig.")
    _log("Optional: " + str(WORKSPACE_DIR / "identity" / "pii_blocklist.txt") + " fuer deterministische Garantie-Begriffe.")


if __name__ == "__main__":
    main()
