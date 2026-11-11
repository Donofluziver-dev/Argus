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
import ipaddress
import json
import logging
import math
import os
import re
from collections import Counter
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

#--- Interne Netzbereiche und Adressen
_INTERNAL_IPV4_NETS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
)

_INTERNAL_IPV6_NETS = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![0-9a-zA-Z:])(?:(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}|"
    r"(?:[0-9a-fA-F]{1,4}:)+:(?:[0-9a-fA-F]{1,4}:)*[0-9a-fA-F]{1,4}|"
    r"::(?:[0-9a-fA-F]{1,4}:)*[0-9a-fA-F]{1,4}|"
    r"(?:[0-9a-fA-F]{1,4}:)+::|::1)(?![0-9a-zA-Z:])"
)

#--- Dienstnamen wie 'llm', 'postgres' oder 'phoenix' sind auch gewoehnliche
#--- Woerter ("Wie funktioniert ein LLM?"). Sie zaehlen nur dort als Host, wo
#--- sie wie einer stehen: in einer URL oder mit Port. Eindeutige interne Namen
#--- (host.docker.internal, *.local/.lan/.internal) immer.
_SERVICE_NAMES = (r"localhost|qdrant|postgres|searxng|llm|rag-backend|stt-service|"
                  r"tts-service|calc-sandbox|phoenix")
_INTERNAL_HOSTNAME_RE = re.compile(
    r"(?i)(?<![a-zA-Z0-9_-])(?:"
    r"host\.docker\.internal|"
    r"rag-backend|stt-service|tts-service|calc-sandbox|"
    r"(?<=://)(?:" + _SERVICE_NAMES + r")|"
    r"(?:" + _SERVICE_NAMES + r")(?=:\d{2,5}\b)|"
    r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)*\.(?:local|lan|internal)"
    r")(?![a-zA-Z0-9_-])"
)

# Unbeschriftete Hochentropie-Tokens (>= 32 Zeichen aus [A-Za-z0-9_-+/=], Shannon >= 4.0, keine reine Hex-Hashes < 41)
_HIGH_ENTROPY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{32,}(?![A-Za-z0-9_\-+/=])")


def _is_internal_ipv4(addr_str: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr_str)
        return any(ip in net for net in _INTERNAL_IPV4_NETS)
    except ValueError:
        return False


def _is_internal_ipv6(addr_str: str) -> bool:
    try:
        ip = ipaddress.IPv6Address(addr_str)
        return any(ip in net for net in _INTERNAL_IPV6_NETS)
    except ValueError:
        return False


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _is_high_entropy_secret(token: str) -> bool:
    if len(token) < 32:
        return False
    # Keine reinen Hex-Hashes von Git-Commits < 41 Zeichen
    if len(token) < 41 and re.fullmatch(r"[0-9a-fA-F]+", token):
        return False
    return _shannon_entropy(token) >= 4.0


def _has_internal_ip(payload: str) -> bool:
    for m in _IPV4_RE.finditer(payload):
        if _is_internal_ipv4(m.group(0)):
            return True
    for m in _IPV6_RE.finditer(payload):
        if _is_internal_ipv6(m.group(0)):
            return True
    return False


def _has_internal_hostname(payload: str) -> bool:
    return bool(_INTERNAL_HOSTNAME_RE.search(payload))


def _has_high_entropy_token(payload: str) -> bool:
    for m in _HIGH_ENTROPY_TOKEN_RE.finditer(payload):
        if _is_high_entropy_secret(m.group(0)):
            return True
    return False

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

    def _mask_high_entropy_secret(m):
        val = m.group(0)
        if _is_high_entropy_secret(val):
            return _placeholder_for(val, "SECRET")
        return val

    out = _HIGH_ENTROPY_TOKEN_RE.sub(_mask_high_entropy_secret, out)

    out = _IBAN_RE.sub(lambda m: _placeholder_for(m.group(0), "IBAN"), out)
    out = _EMAIL_RE.sub(lambda m: _placeholder_for(m.group(0), "EMAIL"), out)
    out = _TEL_RE.sub(lambda m: _placeholder_for(m.group(0), "TEL"), out)
    out = _GEB_RE.sub(lambda m: m.group(1) + _placeholder_for(m.group(2), "DATUM"), out)

    def _mask_ipv4(m):
        ip_str = m.group(0)
        if _is_internal_ipv4(ip_str):
            return _placeholder_for(ip_str, "IP")
        return ip_str

    out = _IPV4_RE.sub(_mask_ipv4, out)

    def _mask_ipv6(m):
        ip_str = m.group(0)
        if _is_internal_ipv6(ip_str):
            return _placeholder_for(ip_str, "IP")
        return ip_str

    out = _IPV6_RE.sub(_mask_ipv6, out)

    def _mask_hostname(m):
        h = m.group(0)
        return _placeholder_for(h, "HOST")

    out = _INTERNAL_HOSTNAME_RE.sub(_mask_hostname, out)

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
    damit das Modul ohne laufenden LLM-Server importierbar bleibt.'''
    global _NER_LLM
    if _NER_LLM is None:
        from rag_backend.crypto_utils import load_llm_api_key
        from rag_backend.utils import make_qwen_llm
        _NER_LLM = make_qwen_llm(
            temperature=0.1, top_p=0.9, max_tokens=1200,
            api_key=load_llm_api_key() or None,
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
    from rag_backend.canary import check_canary
    if check_canary(payload, "Cloud-Anfrage"):
        return "Koeder-Token im Payload"
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
    if _has_high_entropy_token(payload):
        return "Hochentropie-Token/Secret im Payload"
    if _EMAIL_RE.search(payload):
        return "E-Mail-Adresse im Payload"
    if _IBAN_RE.search(payload):
        return "IBAN im Payload"
    if _has_internal_ip(payload):
        return "Interne IP-Adresse im Payload"
    if _has_internal_hostname(payload):
        return "Interner Hostname im Payload"
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
    for ph, original in sorted((mapping or {}).items(), key=lambda kv: len(kv[0]), reverse=True):
        out = out.replace(ph, original)
    return out
