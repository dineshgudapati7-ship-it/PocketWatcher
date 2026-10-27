"""Encryption for secrets at rest (service principal secrets, webhook URLs)."""

from cryptography.fernet import Fernet

from pocketwatcher.settings import get_settings


def _fernet(key: str | None = None) -> Fernet:
    key = key or get_settings().fernet_key
    if not key:
        raise RuntimeError("PW_FERNET_KEY is not set")
    return Fernet(key.encode())


def encrypt(plaintext: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(token: str, key: str | None = None) -> str:
    return _fernet(key).decrypt(token.encode()).decode()
