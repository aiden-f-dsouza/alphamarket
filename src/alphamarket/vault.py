"""Credential vault v2: envelope encryption behind a KeyWrapper (platform_plan 2.4).

Invariant #4: venue credentials are encrypted at rest, never logged, never
committed, never returned by any API.

Envelope scheme, per record:
    data key (DEK)  = a fresh Fernet key (AES-128-CBC + HMAC-SHA256)
    ciphertext      = Fernet(DEK).encrypt(secret)
    wrapped_dek     = KeyWrapper.wrap(DEK)        (KMS, or Fernet on VAULT_KEY)
    key_version     = which wrapping key produced wrapped_dek

The plaintext DEK exists only in memory while one record is sealed or
opened. A database dump alone decrypts nothing; with the AWS wrapper, neither
does a dump plus the app's environment, because unwrapping needs a live KMS
call that IAM can revoke and CloudTrail logs.

Key versions:
    "legacy"            v1 row: ciphertext = Fernet(VAULT_KEY)(secret), no DEK.
                        Only the M1-M7 column and its data migration make these.
    "local:<fp>"        DEK wrapped by MultiFernet(VAULT_KEY keys); <fp> is a
                        fingerprint of the newest key (never the key itself).
    "aws_kms:<key id>"  DEK wrapped by that KMS key.

Rotation (credentials.rotate_all): prepend a new key to VAULT_KEY (or point
KMS_KEY_ID at a new key), run the rotation, then remove the old key. Rows are
re-wrapped one at a time and each read routes by its own key_version, so
reads never stop working during a rotation.

`encrypt_secret` / `decrypt_secret` stay for single-blob callers (TOTP
secrets): the blob is self-contained ("kv2.<version>.<wrapped>.<ciphertext>"),
and `decrypt_secret` still reads v1 single-Fernet blobs.
"""

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from alphamarket.config import get_settings

LEGACY_VERSION = "legacy"
_BLOB_PREFIX = b"kv2."


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sealed:
    """One encrypted record. `refresh_ciphertext` (OAuth) shares the DEK."""

    ciphertext: bytes
    wrapped_dek: bytes | None
    key_version: str
    refresh_ciphertext: bytes | None = None


@runtime_checkable
class KeyWrapper(Protocol):
    """Wraps and unwraps per-record data keys. Implementations never see a
    credential, only DEKs."""

    kind: str  # "local" | "aws_kms"; the key_version prefix

    @property
    def version(self) -> str:
        """key_version newly wrapped DEKs get; rows with another version are
        due for rotation."""

    def handles(self, key_version: str) -> bool:
        """True if this wrapper can unwrap DEKs of `key_version`."""

    def new_data_key(self) -> tuple[bytes, bytes]:
        """(Fernet DEK, wrapped DEK) for a new record."""

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, key_version: str) -> bytes: ...

    def rewrap(self, wrapped: bytes, key_version: str) -> bytes:
        """Re-wrap an existing DEK under `version` (the record ciphertext is
        untouched)."""


def _local_keys() -> list[str]:
    raw = get_settings().vault_key
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise VaultError(
            "VAULT_KEY is not set. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return keys


def key_fingerprint(key: str) -> str:
    """Short, non-reversible label for a wrapping key (safe to store/log)."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class LocalKeyWrapper:
    """Fernet on VAULT_KEY (dev and tests). MultiFernet: the first key wraps,
    every listed key unwraps, rewrap moves a DEK to the first key."""

    kind = "local"

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise VaultError("LocalKeyWrapper needs at least one key")
        try:
            self._multi = MultiFernet([Fernet(k) for k in keys])
        except (ValueError, TypeError) as exc:
            raise VaultError("VAULT_KEY is not a valid Fernet key") from exc
        self._version = f"local:{key_fingerprint(keys[0])}"

    @classmethod
    def from_settings(cls) -> "LocalKeyWrapper":
        return cls(_local_keys())

    @property
    def version(self) -> str:
        return self._version

    def handles(self, key_version: str) -> bool:
        return key_version.startswith("local:")

    def new_data_key(self) -> tuple[bytes, bytes]:
        dek = Fernet.generate_key()
        return dek, self.wrap(dek)

    def wrap(self, dek: bytes) -> bytes:
        return self._multi.encrypt(dek)

    def unwrap(self, wrapped: bytes, key_version: str) -> bytes:
        try:
            return self._multi.decrypt(wrapped)
        except InvalidToken as exc:
            raise VaultError(
                f"cannot unwrap data key ({key_version}): its VAULT_KEY is no longer listed"
            ) from exc

    def rewrap(self, wrapped: bytes, key_version: str) -> bytes:
        try:
            return self._multi.rotate(wrapped)
        except InvalidToken as exc:
            raise VaultError(f"cannot rotate data key ({key_version})") from exc


class AwsKmsKeyWrapper:
    """AWS KMS: GenerateDataKey for new records, Decrypt to open, ReEncrypt to
    rotate (the DEK never leaves KMS in plaintext during a rotation). The
    32-byte KMS data key is used as the Fernet key (urlsafe base64 of 32
    bytes = 16-byte signing key + 16-byte AES key)."""

    kind = "aws_kms"

    def __init__(self, key_id: str, region: str | None = None, client: Any = None) -> None:
        if not key_id:
            raise VaultError("KMS_KEY_ID is not set")
        self.key_id = key_id
        if client is None:
            import boto3  # deferred: only the KMS deployment needs it at import

            client = boto3.client("kms", region_name=region or None)
        self._client = client
        self._resolved: str | None = None

    @property
    def version(self) -> str:
        """aws_kms:<key ARN>. An alias is resolved (DescribeKey, once): the
        stored version must name the key that actually wrapped the DEK, or
        re-pointing the alias would leave rows that Decrypt(KeyId=alias)
        refuses (IncorrectKeyException) and that rotate_all thinks are current."""
        if self._resolved is None:
            if "alias/" in self.key_id:
                meta = self._client.describe_key(KeyId=self.key_id)["KeyMetadata"]
                self._resolved = meta["Arn"]
            else:
                self._resolved = self.key_id
        return f"aws_kms:{self._resolved}"

    def handles(self, key_version: str) -> bool:
        return key_version.startswith("aws_kms:")

    @staticmethod
    def _key_of(key_version: str) -> str:
        return key_version.split(":", 1)[1]

    def new_data_key(self) -> tuple[bytes, bytes]:
        resp = self._client.generate_data_key(KeyId=self.key_id, KeySpec="AES_256")
        return base64.urlsafe_b64encode(resp["Plaintext"]), resp["CiphertextBlob"]

    def wrap(self, dek: bytes) -> bytes:
        resp = self._client.encrypt(KeyId=self.key_id, Plaintext=base64.urlsafe_b64decode(dek))
        return resp["CiphertextBlob"]

    def unwrap(self, wrapped: bytes, key_version: str) -> bytes:
        try:
            resp = self._client.decrypt(CiphertextBlob=wrapped, KeyId=self._key_of(key_version))
        except Exception as exc:  # botocore ClientError and friends; no secret in them
            raise VaultError(f"KMS could not unwrap data key ({type(exc).__name__})") from exc
        return base64.urlsafe_b64encode(resp["Plaintext"])

    def rewrap(self, wrapped: bytes, key_version: str) -> bytes:
        resp = self._client.re_encrypt(
            CiphertextBlob=wrapped,
            SourceKeyId=self._key_of(key_version),
            DestinationKeyId=self.key_id,
        )
        return resp["CiphertextBlob"]


# ---- wrapper selection ---------------------------------------------------------

_override: KeyWrapper | None = None
_kms_cache: dict[tuple[str, str], AwsKmsKeyWrapper] = {}


def use_key_wrapper(wrapper: KeyWrapper | None) -> None:
    """Pin the current wrapper (tests, scripts). None restores settings."""
    global _override
    _override = wrapper


def _kms(key_id: str) -> AwsKmsKeyWrapper:
    region = get_settings().aws_region
    cache_key = (key_id, region)
    if cache_key not in _kms_cache:
        _kms_cache[cache_key] = AwsKmsKeyWrapper(key_id, region)
    return _kms_cache[cache_key]


def current_wrapper() -> KeyWrapper:
    if _override is not None:
        return _override
    s = get_settings()
    if s.key_wrapper == "local":
        return LocalKeyWrapper.from_settings()
    if s.key_wrapper == "aws_kms":
        return _kms(s.kms_key_id)
    raise VaultError(f"unknown KEY_WRAPPER {s.key_wrapper!r} (expected local | aws_kms)")


def wrapper_for(key_version: str) -> KeyWrapper:
    """The wrapper that can unwrap `key_version`, so rows wrapped before a
    wrapper switch (local -> KMS, KMS key A -> B) keep decrypting."""
    current = current_wrapper()
    if current.handles(key_version):
        return current
    if key_version.startswith("local:"):
        return LocalKeyWrapper.from_settings()
    if key_version.startswith("aws_kms:"):
        return _kms(key_version.split(":", 1)[1])
    raise VaultError(f"unknown key version {key_version!r}")


def current_version() -> str:
    return current_wrapper().version


# ---- records ---------------------------------------------------------------------


def seal(plaintext: str, refresh: str | None = None, wrapper: KeyWrapper | None = None) -> Sealed:
    """Encrypt one record under a fresh data key."""
    w = wrapper or current_wrapper()
    dek, wrapped = w.new_data_key()
    f = Fernet(dek)
    return Sealed(
        ciphertext=f.encrypt(plaintext.encode()),
        wrapped_dek=wrapped,
        key_version=w.version,
        refresh_ciphertext=f.encrypt(refresh.encode()) if refresh is not None else None,
    )


def _legacy_fernet() -> MultiFernet:
    try:
        return MultiFernet([Fernet(k) for k in _local_keys()])
    except (ValueError, TypeError) as exc:
        raise VaultError("VAULT_KEY is not a valid Fernet key") from exc


def _record_fernet(wrapped_dek: bytes | None, key_version: str) -> Fernet | MultiFernet:
    if key_version == LEGACY_VERSION:
        return _legacy_fernet()
    if wrapped_dek is None:
        raise VaultError(f"record with key version {key_version!r} has no wrapped data key")
    return Fernet(wrapper_for(key_version).unwrap(wrapped_dek, key_version))


def open_sealed(ciphertext: bytes, wrapped_dek: bytes | None, key_version: str) -> str:
    """Decrypt one record. Callers outside this module go through
    credentials.py, which audits every decrypt."""
    try:
        return _record_fernet(wrapped_dek, key_version).decrypt(ciphertext).decode()
    except InvalidToken as exc:
        raise VaultError("Stored credential cannot be decrypted (wrong key?)") from exc


def open_record(
    ciphertext: bytes,
    wrapped_dek: bytes | None,
    key_version: str,
    refresh_ciphertext: bytes | None = None,
) -> tuple[str, str | None]:
    """(secret, refresh) with one unwrap of the shared data key."""
    f = _record_fernet(wrapped_dek, key_version)
    try:
        secret = f.decrypt(ciphertext).decode()
        refresh = f.decrypt(refresh_ciphertext).decode() if refresh_ciphertext else None
    except InvalidToken as exc:
        raise VaultError("Stored credential cannot be decrypted (wrong key?)") from exc
    return secret, refresh


def rewrap(wrapped_dek: bytes, key_version: str) -> tuple[bytes, str]:
    """Move a data key to the current wrapping key: (new wrapped, new version).
    Same wrapper kind -> native rotate (MultiFernet.rotate / KMS ReEncrypt);
    across kinds -> unwrap with the old wrapper, wrap with the new."""
    target = current_wrapper()
    if target.handles(key_version):
        return target.rewrap(wrapped_dek, key_version), target.version
    dek = wrapper_for(key_version).unwrap(wrapped_dek, key_version)
    return target.wrap(dek), target.version


def self_test() -> None:
    """Round-trip through the current wrapper (preflight). Raises VaultError."""
    sealed = seal("koine-vault-self-test")
    if open_sealed(sealed.ciphertext, sealed.wrapped_dek, sealed.key_version) != (
        "koine-vault-self-test"
    ):
        raise VaultError("vault self-test round trip mismatch")


# ---- single-blob API (v1 callers: TOTP secrets, tests) ----------------------------


def _b64(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _unb64(b: bytes) -> bytes:
    return base64.urlsafe_b64decode(b + b"=" * (-len(b) % 4))


def encrypt_secret(plaintext: str) -> bytes:
    """Self-contained envelope blob: kv2.<version>.<wrapped dek>.<fernet token>."""
    s = seal(plaintext)
    assert s.wrapped_dek is not None
    return b".".join(
        [_BLOB_PREFIX.rstrip(b"."), _b64(s.key_version.encode()), _b64(s.wrapped_dek), s.ciphertext]
    )


def decrypt_secret(token: bytes) -> str:
    """Reads kv2 blobs and v1 single-Fernet blobs (Fernet on VAULT_KEY)."""
    if token.startswith(_BLOB_PREFIX):
        try:
            _, version, wrapped, ciphertext = token.split(b".", 3)
            key_version = _unb64(version).decode()
            wrapped_dek = _unb64(wrapped)
        except ValueError as exc:
            raise VaultError("malformed vault blob") from exc
        return open_sealed(ciphertext, wrapped_dek, key_version)
    return open_sealed(token, None, LEGACY_VERSION)
