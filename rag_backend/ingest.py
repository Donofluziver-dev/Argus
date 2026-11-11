import os
import glob
import uuid
import time
import hashlib
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import qdrant_client
from bs4 import BeautifulSoup
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client.http.models import PointStruct, Filter, FieldCondition, MatchValue
import docx
import pypdfium2 as pdfium

# Fallbacks spiegeln Setup2.LLM_CONFIG und muessen mit dem on-demand-Ingest
# in agent.py uebereinstimmen.
CHUNK_SIZE    = int(os.getenv("CHUNK_SIZE", 1536))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", 192))
COLLECTION    = os.getenv("QDRANT_COLLECTION", "docs")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", 1024))

QDRANT_BATCH_SIZE = int(os.getenv("QDRANT_BATCH_SIZE", 256))
NUM_INGESTION_WORKERS = int(os.getenv("NUM_INGESTION_WORKERS", min(12, os.cpu_count() or 1)))
MAX_FILE_SIZE_MB = 100

_QDRANT_CLIENT: Optional[qdrant_client.QdrantClient] = None


def _get_qdrant() -> qdrant_client.QdrantClient:
    global _QDRANT_CLIENT
    if _QDRANT_CLIENT is None:
        if os.getenv("QDRANT_IN_MEMORY", "false").lower() == "true":
            print("  Qdrant wird im In-Memory-Modus initialisiert (Tests).")
            _QDRANT_CLIENT = qdrant_client.QdrantClient(location=":memory:")
        else:
            _QDRANT_CLIENT = qdrant_client.QdrantClient(url=os.getenv("QDRANT_URL", "http://qdrant:6333"))
    return _QDRANT_CLIENT


def ensure_qdrant_collection(client, collection_name: str, dim: int) -> bool:
    '''Legt die Collection im Hybrid-Aufbau an (dichter Vektor + BM25) oder zieht
    einen alten Aufbau um; True = neu erstellt oder umgezogen.
    ZENTRAL fuer Batch-Ingest UND main.py-Startup (EINE Quelle der Erstellungslogik,
    die Details stehen in retrieval.py).'''
    from rag_backend.retrieval import ensure_collection

    state = ensure_collection(client, collection_name, dim)
    if state != "vorhanden":
        print(f"  Collection '{collection_name}': {state}.")
    return state != "vorhanden"


def build_splitter(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    '''Einheitlicher Text-Splitter fuer Batch-Ingest UND Tool-Ingest (agent.py
    _store_text_in_qdrant) -- beide chunken dieselbe Collection und muessen
    identisch arbeiten, sonst driftet das Chunking je nach Ingest-Pfad.'''
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", ". ", "! ", "? ", "; ", ": ", ", ", " ", ""],
        is_separator_regex=False,
    )


from rag_backend.database import SessionLocal


def _compute_hash(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_known_hash(filename: str) -> Optional[str]:
    try:
        from rag_backend.models import IngestedDocument
        with SessionLocal() as db:
            record = db.query(IngestedDocument).filter_by(filename=filename).first()
            return record.file_hash if record else None
    except Exception:
        return None


def _save_document_record(filename: str, file_path: str, file_hash: str, chunk_count: int):
    try:
        from rag_backend.models import IngestedDocument
        with SessionLocal() as db:
            record = db.query(IngestedDocument).filter_by(filename=filename).first()
            if record:
                record.file_path = file_path
                record.file_hash = file_hash
                record.chunk_count = chunk_count
                record.ingested_at = datetime.now(timezone.utc)
            else:
                db.add(IngestedDocument(
                    filename=filename,
                    file_path=file_path,
                    file_hash=file_hash,
                    chunk_count=chunk_count,
                ))
            db.commit()
    except Exception as e:
        print(f"  -> Warnung: DB-Tracking fehlgeschlagen fuer {filename}: {e}")


def _delete_document_record(filename: str):
    try:
        from rag_backend.models import IngestedDocument
        with SessionLocal() as db:
            db.query(IngestedDocument).filter_by(filename=filename).delete()
            db.commit()
    except Exception as e:
        print(f"  -> Warnung: DB-Loeschung fehlgeschlagen fuer {filename}: {e}")


def _delete_qdrant_chunks(filename: str):
    try:
        _get_qdrant().delete(
            collection_name=COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(
                    key="original_filename",
                    match=MatchValue(value=filename),
                )]
            ),
        )
    except Exception as e:
        print(f"  -> Warnung: Qdrant-Loeschung fehlgeschlagen fuer {filename}: {e}")


def _check_zip_limits(filepath: Path) -> tuple[bool, str | None]:
    import zipfile
    max_uncompressed_mb = int(os.getenv("INGEST_MAX_UNCOMPRESSED_MB", "200"))
    max_uncompressed_bytes = max_uncompressed_mb * 1024 * 1024
    max_ratio = 100
    max_entries = 10000

    try:
        if not zipfile.is_zipfile(filepath):
            return True, None
        with zipfile.ZipFile(filepath, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > max_entries:
                return False, f"ZIP-Container enthält zu viele Einträge ({len(infolist)} > {max_entries})."

            total_uncompressed = sum(info.file_size for info in infolist)
            if total_uncompressed > max_uncompressed_bytes:
                return False, (
                    f"Entpackte Größe übersteigt Limit von {max_uncompressed_mb} MB: "
                    f"{total_uncompressed / (1024 * 1024):.1f} MB."
                )

            compressed_size = filepath.stat().st_size
            if compressed_size > 0:
                ratio = total_uncompressed / compressed_size
                if ratio > max_ratio:
                    return False, f"ZIP-Kompressionsverhältnis überschritten ({ratio:.1f} > {max_ratio})."
    except Exception as e:
        return False, f"ZIP-Container ungültig oder beschädigt: {e}"
    return True, None


def _read_epub_text(filepath: Path) -> Optional[str]:
    # EPUB ohne Drittpaket: META-INF/container.xml -> OPF ->
    # Spine-Reihenfolge; faellt das aus, alle .xhtml/.html/.htm in Namensreihenfolge.
    import posixpath
    import urllib.parse
    import xml.etree.ElementTree as ET
    import zipfile

    def _local_tag(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    with zipfile.ZipFile(filepath, "r") as zf:
        names = zf.namelist()
        name_map = {n: n for n in names}
        name_map_lower = {n.lower(): n for n in names}

        def _resolve_entry(path_str: str) -> Optional[str]:
            norm = posixpath.normpath(path_str).lstrip("/")
            return name_map.get(norm) or name_map_lower.get(norm.lower())

        doc_entries: list[str] = []
        try:
            container_entry = _resolve_entry("META-INF/container.xml")
            if container_entry:
                c_root = ET.fromstring(zf.read(container_entry))
                opf_path = None
                for el in c_root.iter():
                    if _local_tag(el.tag) == "rootfile" and el.attrib.get("full-path"):
                        opf_path = el.attrib["full-path"]
                        break
                if opf_path:
                    opf_entry = _resolve_entry(opf_path)
                    if opf_entry:
                        opf_dir = posixpath.dirname(opf_entry)
                        opf_root = ET.fromstring(zf.read(opf_entry))
                        manifest: dict[str, str] = {}
                        for el in opf_root.iter():
                            if _local_tag(el.tag) == "item":
                                item_id = el.attrib.get("id")
                                href = el.attrib.get("href")
                                if item_id and href:
                                    clean_href = urllib.parse.unquote(href.split("#", 1)[0])
                                    joined = posixpath.join(opf_dir, clean_href) if opf_dir else clean_href
                                    manifest[item_id] = joined
                        for el in opf_root.iter():
                            if _local_tag(el.tag) == "itemref":
                                idref = el.attrib.get("idref")
                                if idref and idref in manifest:
                                    entry = _resolve_entry(manifest[idref])
                                    if entry and entry not in doc_entries:
                                        doc_entries.append(entry)
        except Exception:
            doc_entries = []

        if not doc_entries:
            doc_entries = sorted(
                n for n in names
                if posixpath.splitext(n)[1].lower() in {".xhtml", ".html", ".htm"}
            )

        book_text = []
        for entry in doc_entries:
            try:
                raw = zf.read(entry).decode("utf-8", errors="ignore")
                soup = BeautifulSoup(raw, "html.parser")
                txt = soup.get_text(separator="\n")
                if txt.strip():
                    book_text.append(txt)
            except Exception:
                continue
        return "\n\n".join(book_text) if book_text else None


def load_file(filepath: Path) -> Optional[str]:
    try:
        if not filepath.is_file():
            print(f"  -> Warnung: {filepath.name} ist kein File. Ueberspringe.")
            return None
        if filepath.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            print(f"  -> Ueberspringe {filepath.name}: > {MAX_FILE_SIZE_MB} MB.")
            return None

        suffix = filepath.suffix.lower()
        if suffix in {".docx", ".xlsx", ".pptx", ".epub"}:
            ok, err = _check_zip_limits(filepath)
            if not ok:
                print(f"  -> Ueberspringe {filepath.name} (Sicherheitsgrenze): {err}")
                return None

        text_content = None

        if suffix == ".pdf":
            try:
                doc = pdfium.PdfDocument(str(filepath))
                parts = []
                for i in range(len(doc)):
                    page = doc[i]
                    tp = page.get_textpage()
                    parts.append(tp.get_text_range())
                    tp.close()
                    page.close()
                doc.close()
                text_content = "\n".join(parts)
            except Exception:
                import pdfplumber
                with pdfplumber.open(filepath) as pdf:
                    text_content = "\n".join(page.extract_text() or "" for page in pdf.pages)

        elif suffix == ".docx":
            text_content = "\n".join(p.text for p in docx.Document(filepath).paragraphs)

        elif suffix in {".html", ".htm"}:
            soup = BeautifulSoup(filepath.read_text("utf-8", errors="ignore"), "html.parser")
            text_content = soup.get_text(separator="\n")

        elif suffix == ".epub":
            try:
                text_content = _read_epub_text(filepath)
            except Exception as e:
                print(f"  -> EPUB-Fehler {filepath.name}: {e}")
                return None

        elif suffix == ".pptx":
            from pptx import Presentation
            folien = []
            for nr, folie in enumerate(Presentation(str(filepath)).slides, 1):
                teile = [sh.text for sh in folie.shapes
                         if getattr(sh, "has_text_frame", False) and sh.text.strip()]
                if teile:
                    folien.append(f"--- Folie {nr} ---\n" + "\n".join(teile))
            text_content = "\n\n".join(folien)

        elif suffix == ".xlsx":
            import openpyxl
            # read_only spart Speicher bei grossen Mappen, data_only
            # liefert berechnete Werte statt Formeltexten.
            wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
            blaetter = []
            for ws in wb.worksheets:
                zeilen = []
                for row in ws.iter_rows(values_only=True):
                    zellen = [str(c) for c in row if c is not None]
                    if zellen:
                        zeilen.append("\t".join(zellen))
                if zeilen:
                    blaetter.append(f"--- Blatt: {ws.title} ---\n" + "\n".join(zeilen))
            wb.close()
            text_content = "\n\n".join(blaetter)

        # .env/.log bewusst NICHT in der Allow-List: solche Dateien enthalten
        # haeufig Secrets.
        elif suffix in {".txt", ".md", ".json", ".yaml", ".yml", ".csv",
                        ".py", ".ini", ".cfg"}:
            text_content = filepath.read_text("utf-8", errors="ignore")

        else:
            print(f"  -> Unbekannter Dateityp uebersprungen: {filepath.name}")
            return None

        if text_content is None:
            # Leeres Ergebnis abfangen, sonst wirft len(None) einen TypeError.
            print(f"  -> Kein Textinhalt extrahiert: {filepath.name}")
            return None

        max_text_chars = int(os.getenv("INGEST_MAX_TEXT_CHARS", "5000000"))
        if len(text_content) > max_text_chars:
            print(f"  -> Textinhalt von {filepath.name} überschreitet {max_text_chars} Zeichen, wird gekürzt.")
            text_content = text_content[:max_text_chars]

        kb = filepath.stat().st_size / 1024
        print(f"  -> Geladen: {filepath.name} ({kb:.1f} KB, {len(text_content)} Zeichen)")
        return text_content

    except Exception as e:
        print(f"  -> Fehler beim Laden von {filepath.name}: {e}")
        return None


def _process_single_file_for_ingestion(args):
    f_path_str, db_filename, chunk_size, chunk_overlap = args
    _path = Path(f_path_str)
    text = load_file(_path)
    if not text or not text.strip():
        return [], [], 0, 1, 0
    splitter = build_splitter(chunk_size, chunk_overlap)
    chunks = splitter.split_text(text)
    metadata = [
        {"source": f_path_str, "original_filename": db_filename, "chunk_index": i}
        for i, _ in enumerate(chunks)
    ]
    return chunks, metadata, len(text), 0, 1


def upsert_in_batches(client, collection_name, points, batch_size):
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection_name, points=points[i:i + batch_size], wait=True)


def ingest(data_path="documents_to_ingest"):
    from rag_backend import retrieval

    start_time = time.time()
    print(f"\n--- Starte Ingestion fuer Pfad: '{data_path}' ---")

    if ensure_qdrant_collection(_get_qdrant(), COLLECTION, EMBEDDING_DIM):
        print(f"  Collection '{COLLECTION}' nicht gefunden -- neu erstellt.")
    else:
        print(f"  Collection '{COLLECTION}' in Qdrant gefunden.")

    Path(data_path).mkdir(exist_ok=True)
    files = [f for f in glob.glob(f"{data_path}/**/*", recursive=True) if Path(f).is_file()]
    if not files:
        print(f"  Keine Dateien in '{data_path}'. Abbruch.")
        return
    print(f"  {len(files)} Dateien gefunden.")

    def _db_filename_for(filepath: Path) -> str:
        # DB-/Qdrant-Schluessel wie beim Ordner-Ingest: Relativpfad mit
        # '_'-Separatoren. Der nackte Dateiname kollidierte bei gleichnamigen
        # Dateien in verschiedenen Unterordnern.
        try:
            rel = filepath.relative_to(data_path)
        except ValueError:
            rel = Path(filepath.name)
        return str(rel).replace("/", "_").replace("\\", "_")

    files_to_process = []
    skipped_unchanged = 0
    for f in files:
        filepath = Path(f)
        db_filename = _db_filename_for(filepath)
        current_hash = _compute_hash(filepath)
        known_hash = _get_known_hash(db_filename)
        if known_hash and known_hash == current_hash:
            print(f"  -> Unveraendert: {db_filename} -- uebersprungen.")
            skipped_unchanged += 1
        else:
            if known_hash:
                print(f"  -> Geaendert: {db_filename} -- wird neu indexiert.")
            files_to_process.append((f, db_filename, current_hash))

    if not files_to_process:
        print(f"  Alle {skipped_unchanged} Dateien unveraendert. Nichts zu tun.")
        return

    print(f"  {len(files_to_process)} Datei(en) werden verarbeitet, {skipped_unchanged} unveraendert.")

    task_args = [(f, db_filename, CHUNK_SIZE, CHUNK_OVERLAP) for f, db_filename, _ in files_to_process]
    print(f"  Starte parallele Verarbeitung mit {NUM_INGESTION_WORKERS} Worker(n)...")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(NUM_INGESTION_WORKERS) as pool:
        results = pool.map(_process_single_file_for_ingestion, task_args)

    total_chars = 0
    skipped_empty = 0
    processed = 0

    for (f_path_str, db_filename, file_hash), (chunks, meta, chars, s, p) in zip(files_to_process, results):
        filepath = Path(f_path_str)
        skipped_empty += s
        if not chunks:
            continue

        # Erst einbetten (Container embed), dann loeschen: faellt der Dienst
        # aus, bleibt der alte Stand im Index.
        print(f"  -> Generiere Embeddings fuer {db_filename}...")
        vectors = retrieval.embed_sync(retrieval.texts_for_embedding(db_filename, chunks))
        points = retrieval.build_points(db_filename, f_path_str, chunks, vectors, extra_payload=meta)

        print(f"  -> Delete-before-Upsert: {db_filename} ({len(chunks)} Chunks)...")
        _delete_qdrant_chunks(db_filename)
        if db_filename != filepath.name:
            # Einmalige Migration: Altbestand lief unter dem nackten Namen.
            _delete_qdrant_chunks(filepath.name)
            _delete_document_record(filepath.name)
        upsert_in_batches(_get_qdrant(), COLLECTION, points, QDRANT_BATCH_SIZE)
        _save_document_record(db_filename, f_path_str, file_hash, len(chunks))

        total_chars += chars
        processed += 1

    dur = time.time() - start_time
    print(f"\n--- Ingestion Summary: {processed} neu indexiert, "
          f"{skipped_unchanged} unveraendert, {skipped_empty} leer/fehler, "
          f"{total_chars} Zeichen, {dur:.1f}s ---")


def list_indexed_documents():
    try:
        from rag_backend.models import IngestedDocument
        with SessionLocal() as db:
            records = db.query(IngestedDocument).order_by(IngestedDocument.ingested_at.desc()).all()
            if not records:
                print("  Keine Dokumente indexiert.")
                return
            print(f"  {'Dateiname':<40} {'Chunks':>6}  {'Indexiert am'}")
            print(f"  {'-'*40} {'-'*6}  {'-'*20}")
            for r in records:
                ts = r.ingested_at.strftime('%Y-%m-%d %H:%M') if r.ingested_at else 'unbekannt'
                print(f"  {r.filename:<40} {r.chunk_count or 0:>6}  {ts}")
    except Exception as e:
        print(f"  Fehler beim Abrufen der Dokumente: {e}")


def delete_document(filename: str):
    print(f"  Loesche '{filename}' aus Qdrant und DB...")
    _delete_qdrant_chunks(filename)
    _delete_document_record(filename)
    print(f"  '{filename}' erfolgreich geloescht.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "list":
            list_indexed_documents()
        elif sys.argv[1] == "delete" and len(sys.argv) > 2:
            delete_document(sys.argv[2])
        else:
            print("Verwendung: python ingest.py [list | delete <dateiname>]")
    else:
        ingest()
