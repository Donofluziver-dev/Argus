"""Schwaerzung der Traces, bevor sie Phoenix erreichen.

Phoenix speichert sonst Prompts, Antworten und Werkzeugausgaben im Klartext,
also auch Firmendaten und Schluessel, die zufaellig in einem Dokument stehen.
Der Exporter hier sitzt zwischen OpenTelemetry und dem OTLP-Versand und
ersetzt jede Zeichenkette in Attributen, Ereignissen und Status, bevor sie das
Backend verlaesst.

TRACE_CONTENT (Umgebung):
- "redacted" (Standard): Inhalte bleiben lesbar, aber Schluessel, Tokens,
  E-Mail, IBAN, Telefon, Geburtsdatum, interne IPs/Hostnamen und Begriffe der
  PII-Blocklist werden durch Platzhalter ersetzt. Gleiche Regeln wie vor dem
  Versand an Cloud-Modelle (cloud_agents/redactor.redact_deterministic).
  Namen ohne Blocklist-Eintrag erkennt das nicht.
- "metadata": Prompt-, Antwort-, Dokument- und Werkzeug-Inhalte werden ganz
  ausgeblendet (nur ihre Laenge bleibt); Ablauf, Dauer, Token-Zahlen und
  Werkzeugnamen bleiben sichtbar. Fuer Betrieb mit vielen Nutzern gedacht.

Schlaegt die Schwaerzung fehl, wird der Stapel nicht gesendet (fail closed).
"""

import logging
import os
from typing import Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

from rag_backend.cloud_agents.redactor import redact_deterministic

log = logging.getLogger(__name__)

MODES = ("redacted", "metadata")

# Attribut-Schluessel (OpenInference), die Inhalte tragen. Im Modus metadata
# werden ihre Werte ersetzt; Teilstring-Vergleich, weil Nachrichten indiziert
# sind (llm.input_messages.3.message.content).
_CONTENT_KEY_PARTS = (
    "input.value",
    "output.value",
    "message.content",
    "message_content",
    "message.contents",
    "document.content",
    "llm.prompts",
    "llm.prompt_template",
    "tool.parameters",
    "tool_call.function.arguments",
    "function_call",
    "exception.message",
    "exception.stacktrace",
)


def trace_mode() -> str:
    mode = os.getenv("TRACE_CONTENT", "redacted").strip().lower()
    return mode if mode in MODES else "redacted"


def _is_content_key(key: str) -> bool:
    return any(part in key for part in _CONTENT_KEY_PARTS)


class _Redactor:
    """Haelt das Platzhalter-Mapping je Span: gleiche Werte bekommen innerhalb
    eines Spans denselben Platzhalter. Das Mapping verlaesst den Prozess nie."""

    def __init__(self, mode: str):
        self.mode = mode
        self.mapping: dict = {}

    def text(self, value: str) -> str:
        out, self.mapping = redact_deterministic(value, self.mapping)
        return out

    def value(self, key: str, value):
        if isinstance(value, str):
            if self.mode == "metadata" and _is_content_key(key):
                return f"[ausgeblendet, {len(value)} Zeichen]"
            return self.text(value)
        if isinstance(value, (list, tuple)):
            return tuple(self.value(key, v) for v in value)
        return value

    def attributes(self, attrs) -> dict:
        return {k: self.value(k, v) for k, v in (attrs or {}).items()}


def redact_span(span: ReadableSpan, mode: str | None = None) -> ReadableSpan:
    """Kopie des Spans mit geschwaerzten Zeichenketten."""
    r = _Redactor(mode or trace_mode())
    events = [
        Event(name=ev.name, attributes=r.attributes(ev.attributes), timestamp=ev.timestamp)
        for ev in (span.events or ())
    ]
    status = span.status
    if status is not None and status.description:
        description = (
            f"[ausgeblendet, {len(status.description)} Zeichen]"
            if r.mode == "metadata"
            else r.text(status.description)
        )
        status = Status(status.status_code, description)
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=r.attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Schwaerzt jeden Span, bevor der innere Exporter ihn versendet."""

    def __init__(self, inner: SpanExporter, mode: str | None = None):
        self._inner = inner
        self._mode = mode

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            cleaned = [redact_span(s, self._mode) for s in spans]
        except Exception as e:  # lieber nichts senden als ungeschwaerzt
            log.warning(f"Trace-Schwaerzung fehlgeschlagen ({type(e).__name__}) -- {len(spans)} Spans verworfen.")
            return SpanExportResult.FAILURE
        return self._inner.export(cleaned)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def build_phoenix_tracer_provider(endpoint: str, project_name: str, mode: str | None = None):
    """TracerProvider, der gebuendelt und geschwaerzt an Phoenix (OTLP/HTTP) sendet.

    Ersetzt phoenix.otel.register(): dort laesst sich kein Exporter
    dazwischenschalten."""
    from openinference.semconv.resource import ResourceAttributes
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({ResourceAttributes.PROJECT_NAME: project_name}))
    exporter = RedactingSpanExporter(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"), mode)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider
