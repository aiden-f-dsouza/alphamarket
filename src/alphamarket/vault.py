"""Credential vault: Fernet (AES-128-CBC + HMAC) encryption for venue secrets.

Invariant #4: venue credentials are encrypted at rest, never logged, never
committed, never returned by any API. This is not an HSM, and the methodology
page says so honestly (BUILD_PLAN 2.1). Master key lives in the VAULT_KEY env
secret; losing it means every user must reconnect their venue key, which is
annoying but safe.
"""

from cryptography.fernet import Fernet, InvalidToken

from alphamarket.config import get_settings


class VaultError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().vault_key
    if not key:
        raise VaultError(
            "VAULT_KEY is not set. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key)


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(token: bytes) -> str:
    try:
        return _fernet().decrypt(token).decode()
    except InvalidToken as exc:
        raise VaultError("Stored credential cannot be decrypted (wrong VAULT_KEY?)") from exc
