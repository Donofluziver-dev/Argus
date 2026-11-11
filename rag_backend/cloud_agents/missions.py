'''Missions-Engine (Setup4): Cloud-Agenten arbeiten in Schleifen.

Ablauf pro Mission (Hintergrund-Task in main.py, Muster _reflection_idle_watcher):
  Planner  (lokal, Qwen)   zerlegt das redigierte Ziel in 1-4 Teilaufgaben
  Worker   (Cloud)         bearbeitet Teilaufgaben; vor JEDEM Call: Tripwire
  Reviewer (lokal, Qwen)   ACCEPT oder RETRY mit Feedback (max. Versuche/Budget)
  Synthese (lokal, Qwen)   kombiniert Ergebnisse, Re-Hydration der Platzhalter
Ergebnis Fernet-verschluesselt in der missions-Tabelle, Zustellung in die Ursprungs-Chat-Session.

Alle lokalen LLM-Calls laufen mit tags=[argus_internal], damit sie nie im
User-Stream landen. Alle DB-Zugriffe sind sync (SQLAlchemy) und werden aus
dem Async-Kontext via asyncio.to_thread aufgerufen.
'''
import asyncio
import hashlib
import html
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
# fuer diese IDs die Zustellung; ein Absturz raeumt via finally aus.
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
    '''Interner Call ans lokale Qwen (LLM-Server) -- immer argus_internal getaggt.'''
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


async def _deliver(data: dict, text: str) -> None:
    '''Missions-Meldung "zurueck zur Quelle": in die Chat-Session, aus der die Mission
    kam. Ohne Session (Start ueber die API) bleibt das Ergebnis per mission_status
    abrufbar; einen zweiten Zustellweg gibt es nicht.'''
    d = data or {}
    if d.get("id") in _FOLLOWED and d.get("origin") == "dashboard":
        # Follow-Stream aktiv -- Zustellung wuerde doppelt erfolgen.
        log.info(f"Mission {d.get('id')}: Follower aktiv -- Zustellung uebersprungen.")
        return
    if d.get("origin") == "dashboard" and d.get("dashboard_session_id"):
        # Argus-Chat hat keine gesonderte Push-API: der Verlauf IST die Datenbank. Das
        # Ergebnis als Assistenz-Nachricht in die Ursprungs-Session genuegt.
        if await asyncio.to_thread(_append_dashboard_message,
                                   d.get("dashboard_session_id"), text):
            return
    log.info(f"Mission {d.get('id')}: keine Chat-Session zum Zustellen -- "
             "Ergebnis bleibt per mission_status abrufbar.")


def _append_dashboard_message(session_id, text: str) -> bool:
    '''Missions-Ergebnis als Assistenz-Nachricht in eine Argus-Chat-Session schreiben.

    Sync (ueber asyncio.to_thread aufrufen). Liefert False, wenn die Session nicht
    mehr existiert; das Ergebnis bleibt dann per mission_status abrufbar.'''
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
                   pii_map: dict, provider: str, origin: str = "api",
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
            origin=origin or "api",
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
    from rag_backend.egress_guard import check_egress_allowed, is_in_checked_egress_scope
    egress_block = await check_egress_allowed(
        "start_mission",
        [goal, context or ""],
        force_confirm=not is_in_checked_egress_scope(),
    )
    if egress_block is not None:
        return False, egress_block
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
    channel = origin_info.get("channel") or "api"
    dash_sid = origin_info.get("dashboard_session_id")
    mission_id = await asyncio.to_thread(
        create_mission, USER_ID_DEFAULT, red_goal, red_ctx, mapping,
        provider or os.getenv("CLOUD_PROVIDER_DEFAULT", "gemini"),
        channel, dash_sid,
    )
    in_chat = (channel == "dashboard" and dash_sid)
    where = ("erscheint anschließend hier im Chat" if in_chat
             else "ist danach über mission_status abrufbar")
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
            "origin": getattr(m, "origin", None) or "api",
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
    Format-Quelle fuer Follow-Stream (main.py) und Zustellung.
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


def active_for_dashboard(session_id) -> list:
    '''Aktive Missionen dieser Dashboard-Session. Der Follow-Stream in main.py haengt
    daran, ob er nach der Antwort offen bleibt und den Fortschritt weiterreicht.'''
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
    (Planner/Reviewer laufen auf dem lokalen LLM).
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


_CONTROL_TOKEN_RE = re.compile(
    r"<\s*\|\s*(?:im_start|im_end|endoftext|object_ref_start|object_ref_end|box_start|box_end|quad_start|quad_end|vision_start|vision_end)\s*\|\s*>",
    re.I
)


def wrap_untrusted_cloud_response(text: str) -> str:
    """Spotlighting fuer Cloud-Antworten: rahmt externen Inhalt in XML-Tags,
    entfernt Qwen/ChatML-Steuertokens und schliessende XML-Tags im Inhalt."""
    if not isinstance(text, str) or not text:
        return "<untrusted_cloud_response>\n\n</untrusted_cloud_response>"
    cleaned = _CONTROL_TOKEN_RE.sub("", text)
    cleaned = re.sub(r"<\s*/\s*untrusted_[^>]*>", "", cleaned, flags=re.IGNORECASE)
    escaped = html.escape(cleaned)
    return f"<untrusted_cloud_response>\n{escaped}\n</untrusted_cloud_response>"


# ---------------------------------------------------------------------------
# Einzel-Call (cloud_ask -- ROADMAP Phase 3 "semantischer Router"-Fall)
# ---------------------------------------------------------------------------

async def run_cloud_ask(question: str, context: str, provider_name: str = "") -> str:
    from rag_backend.egress_guard import check_egress_allowed
    egress_block = await check_egress_allowed("cloud_ask", [question, context or ""])
    if egress_block is not None:
        return egress_block
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
    rehydrated = redactor.rehydrate(text, mapping)
    return f"[{provider.name}/{provider.model}] " + wrap_untrusted_cloud_response(rehydrated)
