import os

os.environ.setdefault("PII_BLOCKLIST_PATH", "/nonexistent/pii_blocklist.txt")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from rag_backend import trace_redaction
from rag_backend.trace_redaction import RedactingSpanExporter, build_phoenix_tracer_provider, trace_mode

SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
EMAIL = "max.mustermann@firma.example"
IBAN = "DE89370400440532013000"


def _run(mode, make_span):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(mem, mode)))
    tracer = provider.get_tracer("test")
    make_span(tracer)
    provider.shutdown()
    return mem.get_finished_spans()


def _all_strings(span):
    out = [span.name, span.status.description or ""]
    for v in span.attributes.values():
        out += list(v) if isinstance(v, tuple) else [v]
    for ev in span.events:
        out += [str(v) for v in ev.attributes.values()]
    return " ".join(str(x) for x in out)


def test_redacted_masks_secrets_and_pii_but_keeps_text():
    def make(tracer):
        with tracer.start_as_current_span("llm") as s:
            s.set_attribute("input.value", f"Mein Key ist {SECRET}, Mail {EMAIL}, IBAN {IBAN}. Bitte zusammenfassen.")
            s.set_attribute("llm.token_count.prompt", 123)
            s.set_attribute("tags", ("ok", EMAIL))

    (span,) = _run("redacted", make)
    text = _all_strings(span)
    assert SECRET not in text and EMAIL not in text and IBAN not in text
    assert "Bitte zusammenfassen." in span.attributes["input.value"]
    assert span.attributes["llm.token_count.prompt"] == 123
    assert span.attributes["tags"][0] == "ok"


def test_metadata_hides_content_keeps_structure():
    def make(tracer):
        with tracer.start_as_current_span("tool") as s:
            s.set_attribute("input.value", "Vertrag 4711-2026 mit Kunde Beispiel GmbH")
            s.set_attribute("llm.input_messages.0.message.content", "geheimer Inhalt")
            s.set_attribute("retrieval.documents.0.document.content", "Gehaltsliste")
            s.set_attribute("tool.name", "document_search")
            s.set_attribute("llm.token_count.completion", 42)

    (span,) = _run("metadata", make)
    a = span.attributes
    assert a["input.value"].startswith("[ausgeblendet,")
    assert a["llm.input_messages.0.message.content"].startswith("[ausgeblendet,")
    assert a["retrieval.documents.0.document.content"].startswith("[ausgeblendet,")
    assert a["tool.name"] == "document_search"
    assert a["llm.token_count.completion"] == 42


def test_exception_event_and_status_are_redacted():
    def make(tracer):
        with tracer.start_as_current_span("fail") as s:
            s.record_exception(ValueError(f"Login fehlgeschlagen fuer {EMAIL} mit token={SECRET}"))
            s.set_status(Status(StatusCode.ERROR, f"Fehler bei {EMAIL}"))

    (span,) = _run("redacted", make)
    text = _all_strings(span)
    assert EMAIL not in text and SECRET not in text
    assert span.status.status_code == StatusCode.ERROR


def test_fail_closed_sends_nothing(monkeypatch):
    mem = InMemorySpanExporter()
    exporter = RedactingSpanExporter(mem, "redacted")
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("x") as s:
        s.set_attribute("input.value", EMAIL)
    span = s

    def boom(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(trace_redaction, "redact_deterministic", boom)
    assert exporter.export([span]) == SpanExportResult.FAILURE
    assert mem.get_finished_spans() == ()


def test_mode_from_environment(monkeypatch):
    monkeypatch.delenv("TRACE_CONTENT", raising=False)
    assert trace_mode() == "redacted"
    monkeypatch.setenv("TRACE_CONTENT", "METADATA")
    assert trace_mode() == "metadata"
    monkeypatch.setenv("TRACE_CONTENT", "full")
    assert trace_mode() == "redacted"  # unbekannt -> sicherer Standard, kein Klartext-Modus


def test_provider_uses_project_name_and_redacting_exporter():
    provider = build_phoenix_tracer_provider("http://phoenix:6006", "argus")
    assert provider.resource.attributes.get("openinference.project.name") == "argus"
    processors = provider._active_span_processor._span_processors
    assert any(isinstance(getattr(p, "span_exporter", None) or getattr(getattr(p, "_batch_processor", None), "_exporter", None), RedactingSpanExporter) for p in processors)
    provider.shutdown()
