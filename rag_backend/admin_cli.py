"""Administrations-Werkzeug fuer das Backend (Aufruf im Container).

Aufruf:
    python -m rag_backend.admin_cli reset-password <konto>

Liest das neue Passwort ausschliesslich von der Standardeingabe (nie als
Befehlszeilen-Argument), prueft die Mindestlaenge (8 Zeichen), aktualisiert den
Argon2-Hash, erhoeht token_version (beendet bestehende Sitzungen) und schreibt
einen Audit-Eintrag. Importiert bewusst nicht rag_backend.main.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, TextIO

from argon2 import PasswordHasher

from rag_backend import database
from rag_backend.models import User
from rag_backend.security_audit import audit_auth

_SERVICE_UID = 0
_MIN_PASSWORD_LEN = 8
_ph = PasswordHasher()


def reset_password(
    account: str,
    password: str,
    session_factory: Callable | None = None,
) -> tuple[bool, str]:
    """Setzt das Passwort fuer <account> zurueck. Liefert (Erfolg, Meldung)."""
    name = (account or "").strip()
    if not name:
        return False, "Fehler: Kontoname darf nicht leer sein."

    pw = (password or "").rstrip("\r\n")

    factory = session_factory or database.SessionLocal
    db = factory()
    try:
        target = db.query(User).filter(User.email == name).first()
        if target is None and name == str(_SERVICE_UID):
            target = db.get(User, _SERVICE_UID)

        if target is not None and target.id == _SERVICE_UID:
            return False, "Fehler: Das Dienstkonto (id 0) ist nicht aenderbar."
        if target is None:
            return False, f"Fehler: Unbekanntes Konto '{name}'."

        if len(pw) < _MIN_PASSWORD_LEN:
            return False, f"Fehler: Passwort muss mindestens {_MIN_PASSWORD_LEN} Zeichen lang sein."

        target.hashed_password = _ph.hash(pw)
        current_tv = int(getattr(target, "token_version", 0) or 0)
        target.token_version = current_tv + 1
        db.commit()
        target_id = target.id
        target_email = target.email
    finally:
        db.close()

    audit_auth(
        "account.password_reset",
        status="success",
        user_id=target_id,
        account=target_email,
        account_exists=True,
        details={"source": "cli", "target_id": target_id, "target_account": target_email},
    )
    return True, f"Passwort fuer Konto '{target_email}' wurde zurueckgesetzt."


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.admin_cli",
        description="Administrations-Werkzeug fuer Argus-Konten.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("reset-password", help="Passwort eines Kontos zuruecksetzen (Liest von stdin).")
    rp.add_argument("account", help="Benutzername / E-Mail des Kontos.")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    if args.command == "reset-password":
        stream = stdin if stdin is not None else sys.stdin
        raw_line = stream.readline()
        password = raw_line.rstrip("\r\n")
        ok, msg = reset_password(args.account, password)
        if ok:
            print(msg)
            return 0
        print(msg, file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
