import os
from pathlib import Path
from cryptography.fernet import Fernet

_fernet_instance = None

def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        env_key = os.getenv("FERNET_KEY")
        if env_key:
            _fernet_instance = Fernet(env_key.encode())
        else:
            key_path = Path("/app/fernet_key.txt")
            if not key_path.exists():
                key_path = Path("API_Tokens/fernet_key.txt")
            if not key_path.exists():
                raise RuntimeError("fernet_key.txt nicht gefunden.")
            _fernet_instance = Fernet(key_path.read_bytes().strip())
    return _fernet_instance

def encrypt_msg(text: str) -> bytes:
    if not text:
        return b""
    return _get_fernet().encrypt(text.encode("utf-8"))

def decrypt_msg(data: bytes) -> str:
    if not data:
        return ""
    return _get_fernet().decrypt(data).decode("utf-8")

def load_secret(var_name: str) -> str:
    val = os.getenv(var_name, "")
    if not val:
        # 1. Versuche aus /run/secrets/ zu lesen
        secret_path = Path(f"/run/secrets/{var_name.lower()}")
        if not secret_path.exists():
            # Fallback auf lokale Pfade
            file_map = {
                "POSTGRES_PASSWORD": "postgres_password.txt",
                "HF_TOKEN": "HF_TOKEN.txt",
                "JWT_SECRET": "jwt_secret.txt",
                "LLM_API_KEY": "llm_api_key.txt",
                "SGLANG_API_KEY": "sglang_api_key.txt",
                "AUDIT_HMAC_KEY": "audit_hmac_key.txt",
                "LANGCHAIN_API_KEY": "LANGSMITH_API_KEY.txt",
                "SERVICE_TOKEN": "service_token.txt",
                "GEMINI_API_KEY": "Gemini.txt",
            }
            filename = file_map.get(var_name)
            if filename:
                # Erst /run/secrets/<stem>, dann lokaler API_Tokens-Pfad.
                candidates = [
                    Path("/run/secrets") / Path(filename).stem,
                    Path("API_Tokens") / filename,
                ]
                secret_path = next((p for p in candidates if p.exists()), secret_path)

        if secret_path.exists():
            val = secret_path.read_text(encoding="utf-8").strip()

    if val and val.startswith("ENC:"):
        try:
            return _get_fernet().decrypt(val[4:].encode()).decode()
        except Exception as exc:
            raise RuntimeError(f"Secret {var_name} kann nicht entschluesselt werden.") from exc
    return val

def load_llm_api_key() -> str:
    return load_secret("LLM_API_KEY") or load_secret("SGLANG_API_KEY")

