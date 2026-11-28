"""Dateibereich je Benutzerkonto.

Regelt Ablage, Namensvalidierung, Pfadaufloesung, Quota und Aufraeumen
fuer benutzerbezogene Dateien und Ergebnisse.

Struktur im Arbeitsverzeichnis:
  users/<user_id>/Dateien      (Uploads des Benutzers)
  users/<user_id>/Ergebnisse   (vom Agenten fuer den Benutzer erzeugte Artefakte)
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import shutil
import tempfile
from pathlib import Path
import re
import stat
import unicodedata
import uuid

log = logging.getLogger("argus.user_files")

BEREICH_DIRS: dict[str, str] = {
    "dateien": "Dateien",
    "ergebnisse": "Ergebnisse",
}

_SAFE_FILENAME_RE = re.compile(r"[^\w._ ()\-]")
_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def get_workspace_root() -> Path:
    """Ermittelt das Wurzelverzeichnis des Arbeitsbereichs."""
    return Path(
        os.getenv("ARGUS_WORKSPACE")
        or os.getenv("WORKSPACE_CONTAINER_DIR")
        or "/host/argus_workspace"
    )


def validate_user_id(user_id: int) -> int:
    """Prueft, dass user_id ein echter int > 0 ist (kein bool)."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError(f"Ungueltige user_id: {user_id!r} (muss int > 0 sein)")
    return user_id


def sanitize_filename(raw: str) -> str:
    """Bereinigt einen Dateinamen (Basename) fuer die sichere Ablage.

    - Normalisiert nach Unicode NFC (wichtig fuer macOS NFD).
    - Behaelt Buchstaben und Ziffern aller Schriften sowie . _ ( ) - bei.
    - Ersetzt alle anderen Zeichen durch _.
    - Entfernt fuehrende Punkte sowie abschliessende Punkte und Leerzeichen.
    - Begrenzt auf max. 180 Zeichen und max. 240 Bytes in UTF-8.
    - Weist Windows-Geraetenamen (CON, PRN, AUX, NUL, COM1-9, LPT1-9) und leere Namen ab.
    """
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = name.lstrip(".")
    name = name.rstrip(". ")
    if not name or name in {".", ".."}:
        raise ValueError("Unbrauchbarer Dateiname: Name ist leer.")

    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.rstrip(". ")
    if not name:
        raise ValueError("Unbrauchbarer Dateiname: Keine zulaessigen Zeichen vorhanden.")

    if len(name) > 180:
        name = name[:180]
    while len(name.encode("utf-8")) > 240:
        name = name[:-1]
    name = name.rstrip(". ")
    if not name:
        raise ValueError("Unbrauchbarer Dateiname nach Laengenbegrenzung.")

    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES:
        raise ValueError(f"Unbrauchbarer Dateiname: Windows-Geraetename {stem!r} ist nicht zulaessig.")

    return name


def safe_filename(raw: str) -> str:
    """Baut aus einem beliebigen Namen einen harmlosen Basename ohne Pfadanteile.

    Namen mit ../, %2e%2e, absolutem Pfad, Backslash, Doppelpunkt, Steuerzeichen,
    fuehrendem Punkt oder Windows-Geraetenamen werden strikt abgewiesen.
    """
    raw_str = (raw or "").strip()
    if not raw_str:
        raise ValueError("Unbrauchbarer Dateiname: Name ist leer.")

    # Steuerzeichen strikt abweisen
    if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c).startswith("C") for c in raw_str):
        raise ValueError(f"Ungueltiger Dateiname mit Steuerzeichen: {raw!r}")

    low = raw_str.lower()
    if (
        ".." in raw_str
        or "%2e" in low
        or "/" in raw_str
        or "\\" in raw_str
        or ":" in raw_str
    ):
        raise ValueError(f"Ungueltiger Dateiname mit unzulaessigen Zeichen/Pfaden: {raw!r}")

    if raw_str.startswith("."):
        raise ValueError(f"Ungueltiger Dateiname mit fuehrendem Punkt: {raw!r}")

    return sanitize_filename(raw_str)


def get_user_dir(user_id: int, bereich: str, create: bool = True) -> Path:
    """Liefert das Verzeichnis fuer den angegebenen Bereich eines Kontos.

    bereich: 'dateien' oder 'ergebnisse' (case-insensitive).
    """
    uid = validate_user_id(user_id)
    b_key = (bereich or "").strip().lower()
    if b_key not in BEREICH_DIRS:
        raise ValueError(f"Unbekannter Bereich: {bereich!r}")
    sub_name = BEREICH_DIRS[b_key]

    ws = get_workspace_root().resolve(strict=False)
    user_root = (ws / "users" / str(uid)).resolve(strict=False)
    target = (user_root / sub_name).resolve(strict=False)

    try:
        target.relative_to(user_root)
    except ValueError:
        raise ValueError("Bereichsordner liegt ausserhalb des Benutzerverzeichnisses.")

    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_user_file(user_id: int, bereich: str, filename: str, must_exist: bool = False) -> Path:
    """Loest einen Dateinamen sicher im Bereich des Kontos auf.

    Weist Symlinks strikt ab (auch wenn das Ziel im Bereich liegt).
    """
    safe_name = safe_filename(filename)
    folder = get_user_dir(user_id, bereich, create=False)
    folder_resolved = folder.resolve(strict=False)
    raw_path = folder / safe_name

    # Symlinks strikt ablehnen (auch wenn das Ziel im Bereich liegt)
    if raw_path.is_symlink() or os.path.islink(str(raw_path)):
        raise ValueError("Symlinks sind nicht erlaubt.")

    target = raw_path.resolve(strict=False)
    try:
        target.relative_to(folder_resolved)
    except ValueError:
        raise ValueError("Pfad liegt ausserhalb des Bereichs.")

    if target.is_symlink() or os.path.islink(str(target)):
        raise ValueError("Symlinks sind nicht erlaubt.")

    if must_exist and not target.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {safe_name}")

    return target


_NEW_FILE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                   | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
_NEW_FILE_MODE = 0o600


def _numbered_names(name: str, max_tries: int):
    yield name
    stem, ext = os.path.splitext(name)
    for i in range(2, max_tries):
        yield f"{stem}_{i}{ext}"
    yield f"{stem}_{uuid.uuid4().hex[:8]}{ext}"


def open_new_file(folder: Path, name: str, max_tries: int = 100) -> tuple[Path, int]:
    """Legt eine neue Datei im Ordner exklusiv an und liefert (Pfad, Dateideskriptor).

    O_EXCL statt vorheriger Existenzpruefung: Zwei gleichzeitige Uploads gleichen
    Namens bekommen verschiedene Dateien, keiner ueberschreibt den anderen.
    O_NOFOLLOW: Ein Symlink an dieser Stelle wird nie verfolgt. Ist der Name belegt,
    folgen name_2, name_3 ... und zuletzt ein Zufallszusatz."""
    for cand in _numbered_names(sanitize_filename(name), max_tries):
        path = folder / sanitize_filename(cand)
        try:
            fd = os.open(path, _NEW_FILE_FLAGS, _NEW_FILE_MODE)
        except FileExistsError:
            continue
        return path, fd
    raise FileExistsError(f"Kein freier Dateiname fuer {name!r}.")


def write_new_file(folder: Path, name: str, data: bytes, user_id: int | None = None) -> Path:
    """Schreibt data in eine neue Datei (siehe open_new_file) und liefert ihren Pfad.
    Mit user_id verschluesselt mit dem Schluessel dieses Kontos (account_crypto)."""
    path, fd = open_new_file(folder, name)
    try:
        with os.fdopen(fd, "wb") as fp:
            if user_id is None:
                fp.write(data)
            else:
                from rag_backend.account_crypto import EncryptedFileWriter
                writer = EncryptedFileWriter(fp, user_id)
                writer.write(data)
                writer.close()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def read_user_file(user_id: int, path: Path) -> bytes:
    """Klartext einer Datei aus dem Bereich des Kontos (auch aeltere, unverschluesselte)."""
    from rag_backend.account_crypto import read_file_plaintext
    return read_file_plaintext(path, validate_user_id(user_id))


def iter_user_file(user_id: int, path: Path):
    from rag_backend.account_crypto import iter_file_plaintext
    return iter_file_plaintext(path, validate_user_id(user_id))


def _plaintext_root() -> Path:
    """Ablage fuer Klartext-Kopien, die nur Leseroutinen brauchen (PDF, Word, Excel ...):
    ein tmpfs im Speicher (/run/argus-tmp, siehe docker-compose.yml), sonst das
    temporaere Verzeichnis des Systems."""
    raw = (os.getenv("ARGUS_PLAINTEXT_DIR") or "").strip()
    if raw:
        return Path(raw)
    default = Path("/run/argus-tmp")
    return default if default.is_dir() else Path(tempfile.gettempdir())


@contextmanager
def plaintext_copy(user_id: int, path: Path):
    """Pfad zu einer Klartext-Kopie fuer Routinen, die einen Dateipfad brauchen. Die Kopie
    liegt in einem eigenen Ordner (0700) und verschwindet danach. Aeltere unverschluesselte
    Dateien werden direkt gelesen."""
    from rag_backend.account_crypto import is_encrypted_file
    if not is_encrypted_file(path):
        yield path
        return
    folder = Path(tempfile.mkdtemp(prefix="klartext-", dir=_plaintext_root()))
    try:
        target = folder / path.name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fp:
            for chunk in iter_user_file(user_id, path):
                fp.write(chunk)
        yield target
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def get_user_files_max_bytes() -> int:
    """Maximal zulaessiger Speicherplatz je Konto in Bytes."""
    mb = int(os.getenv("USER_FILES_MAX_MB", "500"))
    return mb * 1024 * 1024


def get_user_used_bytes(user_id: int) -> int:
    """Zaehlt den belegten Speicherplatz (Dateien + Ergebnisse) eines Kontos in Bytes."""
    validate_user_id(user_id)
    total = 0
    for b in ("dateien", "ergebnisse"):
        folder = get_user_dir(user_id, b, create=False)
        if not folder.is_dir():
            continue
        try:
            for p in folder.iterdir():
                try:
                    if p.is_symlink() or os.path.islink(str(p)):
                        continue
                    if p.is_file():
                        #--- Klartextgroesse: Die Grenze gilt fuer das, was hochgeladen wurde,
                        #--- nicht fuer die paar Bytes, die die Verschluesselung dazugibt.
                        from rag_backend.account_crypto import plaintext_size
                        total += plaintext_size(p)
                except OSError:
                    continue
        except OSError:
            continue
    return total


def check_user_quota(user_id: int, additional_bytes: int = 0) -> None:
    """Prueft, ob das Konto mit additional_bytes das Speicherlimit ueberschreitet."""
    used = get_user_used_bytes(user_id)
    limit = get_user_files_max_bytes()
    if used + additional_bytes > limit:
        limit_mb = limit // (1024 * 1024)
        raise ValueError(f"Speicherplatzgrenze von {limit_mb} MB fuer dieses Konto ueberschritten.")


def list_user_files(user_id: int, bereich: str) -> list[dict]:
    """Listet regulaere Dateien im angegebenen Bereich eines Kontos."""
    folder = get_user_dir(user_id, bereich, create=False)
    if not folder.is_dir():
        return []
    entries = []
    try:
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            try:
                if p.is_symlink() or os.path.islink(str(p)):
                    continue
                if not p.is_file():
                    continue
                st = p.stat()
                from rag_backend.account_crypto import plaintext_size
                entries.append({
                    "name": p.name,
                    "size": plaintext_size(p),
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                })
            except OSError:
                continue
    except OSError:
        return []
    return entries


def delete_user_file(user_id: int, bereich: str, filename: str) -> bool:
    """Loescht eine Datei im angegebenen Bereich eines Kontos."""
    target = resolve_user_file(user_id, bereich, filename, must_exist=True)
    target.unlink()
    return True


def sweep_user_files_once(retention_hours: float | None = None) -> int:
    """Entfernt veraltete Dateien in users/*/Dateien und users/*/Ergebnisse.

    Nur diese beiden Ordner werden aufgeraeumt. MEMORY.md und memory/
    bleiben unter allen Umstaenden unangetastet.
    """
    if retention_hours is None:
        try:
            retention_hours = float(os.getenv("USER_FILES_RETENTION_HOURS", "168"))
        except (ValueError, TypeError):
            retention_hours = 168.0

    if retention_hours <= 0:
        return 0

    ws = get_workspace_root().resolve(strict=False)
    users_dir = ws / "users"
    if not users_dir.is_dir():
        return 0

    grenze = datetime.now(timezone.utc).timestamp() - retention_hours * 3600
    weg = 0

    try:
        user_folders = [u for u in users_dir.iterdir() if u.is_dir() and u.name != "_defaults"]
    except OSError:
        return 0

    for u_dir in user_folders:
        for sub_name in ("Dateien", "Ergebnisse"):
            sub_dir = u_dir / sub_name
            if not sub_dir.is_dir():
                continue
            try:
                for p in sub_dir.iterdir():
                    try:
                        if p.is_symlink() or os.path.islink(str(p)):
                            continue
                        if not p.is_file():
                            continue
                        if p.stat().st_mtime < grenze:
                            p.unlink()
                            weg += 1
                            log.info(f"Benutzerdatei aufgeraeumt (aelter als {retention_hours}h): {p.name}")
                    except Exception as e:
                        log.warning(f"Benutzerdatei {p.name} liess sich nicht aufraeumen: {e}")
            except OSError:
                continue

    return weg
