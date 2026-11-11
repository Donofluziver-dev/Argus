"""Prompt-Dateien des Agenten: SOUL.md, SKILL.md, AGENTS.md.

Vorgaben liegen versioniert in prompts/identity/. Eine gleichnamige Datei in
<Workspace>/identity/ ersetzt die Vorgabe (eigene Anpassung). Fehlt beides,
bleibt der Teil leer.

Platzhalter und Bloecke (gelten fuer Vorgaben und eigene Dateien):
  {{OWNER}}                    Anrede der angemeldeten Person, sonst "the user"
  {{#tool:NAME}}...{{/tool:NAME}}     nur wenn das Werkzeug NAME angeboten wird
  {{^tool:NAME}}...{{/tool:NAME}}     nur wenn es fehlt
So beschreibt der Prompt keine Werkzeuge, die es in dieser Installation nicht
gibt; das Modell versucht sie sonst zu benutzen oder behauptet Zugriffe.
Ein unbekannter Schluessel gilt als "nicht vorhanden": aeltere eigene Dateien mit
{{#host_tools}}-Abschnitten verlieren diese also einfach.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent / "identity"
_BLOCK = re.compile(r"\{\{([#^])([\w:]+)\}\}(.*?)\{\{/\2\}\}", re.S)


def load(name: str, workspace_identity: Path) -> tuple[str, str]:
    """(Text, Herkunft) mit Herkunft "workspace", "vorgabe" oder "keine"."""
    own = workspace_identity / name
    if own.exists():
        return own.read_text(encoding="utf-8"), "workspace"
    default = DEFAULT_DIR / name
    if default.exists():
        return default.read_text(encoding="utf-8"), "vorgabe"
    return "", "keine"


def render(text: str, *, tool_names: set[str], owner: str | None = None) -> str:
    def enabled(key: str) -> bool:
        if key.startswith("tool:"):
            return key[5:] in tool_names
        return False

    def sub(m: re.Match) -> str:
        on = enabled(m.group(2))
        return m.group(3) if (on if m.group(1) == "#" else not on) else ""

    prev = None
    while prev != text:  # verschachtelte Bloecke von innen nach aussen
        prev, text = text, _BLOCK.sub(sub, text)
    owner = (owner or "").strip() or "the user"
    text = text.replace("{{OWNER}}", owner)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
