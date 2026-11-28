import os

#--- Ohne diese Vorgabe fragt jeder Test, der Dateien liest oder die Chat-Einstellungen
#--- holt, den (in Tests nicht vorhandenen) LLM-Server nach der Bildeingabe und wartet
#--- bis zum Zeitlimit. Die Tests der Bildeingabe setzen den Schalter selbst.
os.environ.setdefault("LLM_ENABLE_MULTIMODAL", "false")
#--- Die Installation fragt sonst den Docker-Daemon nach seinen Laufzeiten (gVisor). Ein
#--- Testlauf soll nicht davon abhaengen, ob auf dem Rechner runsc installiert ist.
os.environ.setdefault("ARGUS_DOCKER_RUNTIMES", "runc")
#--- Hauptschluessel der Verschluesselung je Konto fuer die Tests (im Betrieb API_Tokens/kek.txt).
os.environ.setdefault("ARGUS_KEK", "dGVzdC1rZWstZnVlci1kaWUtdGVzdHMtMzItYnl0ZXM")
os.environ.setdefault("ACCOUNT_CRYPTO_MARKER", os.path.join(os.path.dirname(__file__), ".account_crypto_test.json"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _account_keys_fresh():
    """Jeder Test bekommt eigene Datenschluessel, verpackt mit dem Test-Hauptschluessel und
    nur im Speicher: Die Tests nutzen verschiedene SQLite-Dateien, und die Schluessel sollen
    nicht davon abhaengen, welche gerade angelegt ist. Den Speicher in der Datenbank
    prueft test_account_crypto.py eigens."""
    from rag_backend import account_crypto
    account_crypto.set_keys(account_crypto.AccountKeys(
        provider=account_crypto.LocalKeyProvider(), store=account_crypto.MemoryKeyStore()))
    yield
    account_crypto.set_keys(None)
