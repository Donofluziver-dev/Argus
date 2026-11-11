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
