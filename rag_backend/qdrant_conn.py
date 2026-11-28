"""Verbindung zu Qdrant, mit API-Key.

Qdrant haengt im Netz "intern" zusammen mit Modell, Sprache und Phoenix. Ohne
Schluessel koennte jeder dieser Dienste, waere er kompromittiert, die ganze
Wissensbasis lesen und veraendern. Der Schluessel steht als QDRANT_API_KEY in der
.env (setup/init_stack.py legt ihn an); derselbe Wert geht an den Qdrant-Container.
Ohne Wert verbindet sich das Backend wie bisher ohne Schluessel.
"""

import os
import warnings

from qdrant_client import QdrantClient


def qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://qdrant:6333")


def qdrant_api_key() -> str | None:
    return (os.getenv("QDRANT_API_KEY") or "").strip() or None


def make_qdrant_client(url: str | None = None) -> QdrantClient:
    """QdrantClient mit dem Schluessel aus der Umgebung. Die Warnung zu HTTP ohne TLS
    ist hier gegenstandslos: Die Verbindung bleibt im internen Docker-Netz."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        return QdrantClient(url=url or qdrant_url(), api_key=qdrant_api_key())
