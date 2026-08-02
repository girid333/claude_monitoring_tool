"""D-023/D-025: encryption-at-rest for credentials stored in the registry
(systems.json) — per-server/per-DB-host SSH passwords (D-023) and SAP WebGUI
login passwords (D-025). Env vars remain a supported fallback for both
classes; see registry.py's RegistryCredentialProvider.

REGISTRY_ENCRYPTION_KEY must be a Fernet key: a 44-character urlsafe-base64
string, e.g. from `python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`. Deliberately never auto-generated
and never has a default — an auto-generated key that's different every
process restart would silently orphan every previously-encrypted password
(worse than not encrypting at all, since there'd be no warning). The
operator generates it once, keeps it safe, and sets it as an env var next
to the existing SAP_*/CORS_ORIGINS/etc. env vars.
"""

from __future__ import annotations

import os
from functools import lru_cache


class EncryptionNotConfigured(RuntimeError):
    """REGISTRY_ENCRYPTION_KEY isn't set. Raised lazily — only when code
    actually tries to encrypt or decrypt something — so the app still starts
    and registries with no stored SSH credentials still work with zero
    encryption setup required."""


@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("REGISTRY_ENCRYPTION_KEY")
    if not key:
        raise EncryptionNotConfigured(
            "REGISTRY_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "then export REGISTRY_ENCRYPTION_KEY=<that value> on the machine "
            "running the backend before storing SSH credentials (D-023). "
            "Keep this key safe — losing it makes every stored SSH password "
            "unrecoverable."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionNotConfigured(
            f"REGISTRY_ENCRYPTION_KEY is set but not a valid Fernet key: {exc}"
        ) from exc


def is_configured() -> bool:
    return bool(os.environ.get("REGISTRY_ENCRYPTION_KEY"))


def encrypt(plaintext: str) -> str:
    """Empty string in, empty string out — callers don't need to special-case
    'no password set' (e.g. key-based auth) before calling this."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()
