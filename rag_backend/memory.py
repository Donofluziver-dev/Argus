import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_instance = None

# Memory-Updater: bekommt die bestehende MEMORY.md plus ein neues Transkript und
# gibt die vollstaendig aktualisierte Datei zurueck (Supersede statt Anhaengen).
_MEMORY_UPDATE_SYSTEM = (
    "You maintain the long-term memory (MEMORY.md) about ONE user. You receive the "
    "EXISTING MEMORY.md and a new conversation transcript. Return the COMPLETE, "
    "updated MEMORY.md (Markdown, terse bullet points). Write entries in English; "
    "keep proper names and quoted user wording as-is. "
    "Rules:\n"
    "1) Store ONLY durable, reusable facts about the user: name, role, technical "
    "environment, way of working, preferences, ongoing projects and hard constraints "
    "(e.g. 'is moving', 'has pets', '60sqm apartment').\n"
    "2) STRICTLY IGNORE one-offs and day-to-day items: small talk, single factual "
    "questions, weather/time/prices, one-time product comparisons or purchase "
    "decisions, and the content of the assistant's answers.\n"
    "3) Resolve CONTRADICTIONS: a new state REPLACES the old one (example: "
    "'move planned' becomes 'move completed'). Remove outdated or completed entries "
    "instead of keeping them side by side.\n"
    "4) No duplicates. Keep existing, still-valid facts unchanged.\n"
    "5) If the transcript changes nothing durable, return the existing MEMORY.md "
    "UNCHANGED.\n"
    "Answer ONLY with the content of MEMORY.md, no preamble and no code fences."
)


class EpisodicMemory:
    """Dateibasiertes Langzeitgedaechtnis pro Nutzer (users/<id>/MEMORY.md im :rw-Workspace).
    Kein Vektorspeicher mehr: die MEMORY.md wird direkt in den System-Prompt geladen
    (get_prompt_from_env -> <longterm_memory>) und per Post-Session-Reflektion mit
    Supersede aktualisiert."""

    def __init__(self):
        self._ws = Path(os.getenv("ARGUS_WORKSPACE", "/host/argus_workspace"))
        self._max_transcript_chars = int(os.getenv("REFLECTION_MAX_CHARS", 12000))
        self._max_memory_chars = int(os.getenv("MEMORY_MAX_CHARS", 6000))
        self._reflect_lock = asyncio.Lock()

    # --- Pfade / Datei-IO ----------------------------------------------
    def _memory_path(self, user_id: int) -> Path:
        return self._ws / "users" / str(user_id) / "MEMORY.md"

    def _read_memory(self, user_id: int) -> str:
        p = self._memory_path(user_id)
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    def _user_exists(self, user_id: int) -> bool:
        from rag_backend.database import SessionLocal
        from rag_backend.models import User
        with SessionLocal() as db:
            return db.get(User, user_id) is not None

    def _write_memory(self, user_id: int, content: str) -> None:
        if not self._user_exists(user_id):
            log.info(f"Reflektion Schreiben unterdrueckt (user={user_id}): Konto existiert nicht mehr.")
            return
        p = self._memory_path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Eine Backup-Generation vor dem Ueberschreiben.
        try:
            if p.exists():
                p.with_suffix(".md.bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        p.write_text(content, encoding="utf-8")

    async def reflect(self, user_id: int) -> int:
        """Post-Session-Reflektion (dateibasiert): liest neue Nachrichten dieses
        Nutzers aus Postgres, fuettert sie mit der bestehenden MEMORY.md in den
        Updater-LLM und schreibt die aktualisierte MEMORY.md zurueck (Supersede).
        Gibt 1 zurueck wenn geschrieben, sonst 0. Vollstaendig fehlertolerant."""
        if self._reflect_lock.locked():
            return 0
        async with self._reflect_lock:
            from rag_backend.agent import _llm_fast, _CHARS_PER_TOKEN
            if _llm_fast is None:
                return 0
            try:
                rows, max_ts = await asyncio.to_thread(self._load_new_messages, user_id)
            except Exception as e:
                log.warning(f"Reflektion: Laden der Nachrichten fehlgeschlagen (user={user_id}): {e}")
                return 0
            if not rows or max_ts is None:
                return 0

            transcript = "\n".join(f"{sender}: {text}" for sender, text in rows)
            if len(transcript) > self._max_transcript_chars:
                transcript = transcript[-self._max_transcript_chars:]

            existing = await asyncio.to_thread(self._read_memory, user_id)
            existing_body = existing.strip() or "(leer)"

            try:
                from langchain_core.messages import SystemMessage, HumanMessage
                # _llm_fast (No-Think): bei _llm_think frisst der <think>-Block das Budget.
                human = (
                    f"=== EXISTING MEMORY.md ===\n{existing_body}\n\n"
                    f"=== NEW TRANSCRIPT ===\n{transcript}"
                )
                # Antwort-Budget an MEMORY_MAX_CHARS koppeln: ein fester Wert
                # schnitt eine fast volle MEMORY.md beim Zurueckgeben ab.
                reply_tokens = int(self._max_memory_chars / _CHARS_PER_TOKEN) + 400
                resp = await _llm_fast.ainvoke(
                    [SystemMessage(content=_MEMORY_UPDATE_SYSTEM), HumanMessage(content=human)],
                    max_tokens=reply_tokens,
                )
                from rag_backend.utils import strip_think
                updated = strip_think(resp.content if isinstance(resp.content, str) else str(resp.content))
                updated = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", updated).strip()
            except Exception as e:
                log.warning(f"Reflektion: LLM-Update fehlgeschlagen (user={user_id}): {e}")
                return 0  # Wasserzeichen NICHT setzen -> naechster Lauf versucht erneut.

            wrote = 0
            prev_len = len(existing.strip())
            # Guardrails: leer verwerfen; bei nicht-trivialem Bestand keine
            # >50%-Schrumpfung akzeptieren.
            if not updated:
                log.warning(f"Reflektion: leere Updater-Antwort (user={user_id}) -- verworfen.")
            elif prev_len > 200 and len(updated) < prev_len * 0.5:
                log.warning(f"Reflektion: Update verdaechtig kurz ({len(updated)} < 50% von {prev_len}, user={user_id}) -- verworfen.")
            elif updated == existing.strip():
                wrote = 0  # nichts Dauerhaftes geaendert
            else:
                if len(updated) > self._max_memory_chars:
                    updated = updated[:self._max_memory_chars]
                if not await asyncio.to_thread(self._user_exists, user_id):
                    log.info(f"Reflektion abgebrochen (user={user_id}): Konto existiert nicht mehr.")
                    return 0
                try:
                    await asyncio.to_thread(self._write_memory, user_id, updated + "\n")
                    wrote = 1
                except Exception as e:
                    log.warning(f"Reflektion: Schreiben der MEMORY.md fehlgeschlagen (user={user_id}): {e}")
                    return 0

            # Wasserzeichen erst nach erfolgreichem (oder bewusst verworfenem) Lauf setzen.
            if not await asyncio.to_thread(self._user_exists, user_id):
                log.info(f"Reflektion abgebrochen (user={user_id}): Konto existiert nicht mehr.")
                return 0
            try:
                await asyncio.to_thread(self._set_watermark, user_id, max_ts)
            except Exception as e:
                log.warning(f"Reflektion: Wasserzeichen-Update fehlgeschlagen (user={user_id}): {e}")

            self._write_reflection_log(user_id, len(rows), wrote)
            log.info(f"Reflektion abgeschlossen (user={user_id}): {len(rows)} Nachrichten -> MEMORY.md {'aktualisiert' if wrote else 'unveraendert'}.")
            return wrote

    # --- interne Helfer (synchron, laufen in to_thread) -----------------

    def _load_new_messages(self, user_id: int):
        from rag_backend.database import SessionLocal
        from rag_backend.models import Message, ChatSession, ReflectionState
        from rag_backend.crypto_utils import decrypt_msg
        with SessionLocal() as db:
            state = db.get(ReflectionState, user_id)
            watermark = state.reflected_until if state else None
            q = (
                db.query(Message)
                .join(ChatSession, Message.session_id == ChatSession.id)
                .filter(ChatSession.user_id == user_id)
            )
            if watermark is not None:
                q = q.filter(Message.timestamp > watermark)
            msgs = q.order_by(Message.timestamp.asc()).all()
            rows, max_ts = [], None
            for m in msgs:
                try:
                    text = decrypt_msg(m.content_encrypted)
                except Exception:
                    continue
                rows.append((m.sender, text))
                if max_ts is None or m.timestamp > max_ts:
                    max_ts = m.timestamp
            return rows, max_ts

    def _set_watermark(self, user_id: int, max_ts):
        from rag_backend.database import SessionLocal
        from rag_backend.models import ReflectionState, User
        with SessionLocal() as db:
            if db.get(User, user_id) is None:
                log.info(f"Reflektion Wasserzeichen unterdrueckt (user={user_id}): Konto existiert nicht mehr.")
                return
            state = db.get(ReflectionState, user_id)
            if state is None:
                state = ReflectionState(user_id=user_id)
                db.add(state)
            state.reflected_until = max_ts
            db.commit()

    def _write_reflection_log(self, user_id: int, n_msgs: int, wrote: int) -> None:
        path = Path(os.getenv("REFLECTION_LOG_PATH", str(self._ws / "logs" / "reflection.log")))
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            line = f"{ts} user={user_id} msgs={n_msgs} memory={'updated' if wrote else 'unchanged'}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as e:
            log.debug(f"reflection.log nicht geschrieben: {e}")


def get_episodic_memory() -> "EpisodicMemory":
    global _instance
    if _instance is None:
        _instance = EpisodicMemory()
    return _instance


async def run_reflection_cycle() -> None:
    """Reflektiert alle Nutzer mit neuen Nachrichten. Wird vom Idle-Watcher
    in main.py aufgerufen. Fehler einzelner Nutzer brechen den Zyklus nicht ab."""
    from rag_backend.database import SessionLocal
    from rag_backend.models import ChatSession

    def _user_ids():
        with SessionLocal() as db:
            return [r[0] for r in db.query(ChatSession.user_id).distinct().all() if r[0] is not None]

    try:
        user_ids = await asyncio.to_thread(_user_ids)
    except Exception as e:
        log.warning(f"Reflektion: Nutzer-Liste konnte nicht geladen werden: {e}")
        return
    mem = get_episodic_memory()
    for uid in user_ids:
        try:
            await mem.reflect(uid)
        except Exception as e:
            log.warning(f"Reflektion fuer user {uid} fehlgeschlagen: {e}")
