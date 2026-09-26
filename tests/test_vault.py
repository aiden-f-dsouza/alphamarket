"""Vault v2: envelope encryption, key wrappers, rotation, v1 blob compatibility.

AWS KMS is exercised through botocore's Stubber: responses are canned, no
request leaves the process (invariant 6).
"""

import base64
import hashlib

import boto3
import pytest
from botocore.stub import Stubber
from cryptography.fernet import Fernet

from alphamarket import vault
from alphamarket.config import get_settings
from alphamarket.vault import AwsKmsKeyWrapper, LocalKeyWrapper, VaultError

KMS_KEY = "alias/koine-vault"
KMS_ARN = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"


@pytest.fixture(autouse=True)
def _no_override():
    vault.use_key_wrapper(None)
    yield
    vault.use_key_wrapper(None)


def _kms_client():
    return boto3.client(
        "kms",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def test_local_seal_is_an_envelope_and_round_trips():
    sealed = vault.seal("my-pem", refresh="my-refresh")
    key = get_settings().vault_key
    # key_version = "local:" + first 12 hex digits of sha256(VAULT_KEY)
    assert sealed.key_version == "local:" + hashlib.sha256(key.encode()).hexdigest()[:12]
    assert b"my-pem" not in sealed.ciphertext
    # The record is NOT encrypted with VAULT_KEY directly: only the DEK is.
    dek = Fernet(key).decrypt(sealed.wrapped_dek)
    assert Fernet(dek).decrypt(sealed.ciphertext) == b"my-pem"
    assert Fernet(dek).decrypt(sealed.refresh_ciphertext) == b"my-refresh"
    assert vault.open_record(
        sealed.ciphertext, sealed.wrapped_dek, sealed.key_version, sealed.refresh_ciphertext
    ) == ("my-pem", "my-refresh")


def test_each_record_gets_its_own_data_key():
    a, b = vault.seal("same"), vault.seal("same")
    key = get_settings().vault_key
    assert Fernet(key).decrypt(a.wrapped_dek) != Fernet(key).decrypt(b.wrapped_dek)


def test_decrypt_secret_reads_v1_single_fernet_blobs():
    legacy = Fernet(get_settings().vault_key).encrypt(b"totp-seed")
    assert vault.decrypt_secret(legacy) == "totp-seed"


def test_encrypt_secret_blob_round_trips_and_is_self_contained():
    blob = vault.encrypt_secret("JBSWY3DPEHPK3PXP")
    assert blob.startswith(b"kv2.") and b"JBSWY3DPEHPK3PXP" not in blob
    assert vault.decrypt_secret(blob) == "JBSWY3DPEHPK3PXP"


def test_wrong_key_raises_vault_error():
    blob = vault.encrypt_secret("x")
    get_settings().vault_key = Fernet.generate_key().decode()
    with pytest.raises(VaultError):
        vault.decrypt_secret(blob)


def test_multifernet_rotation_moves_dek_to_new_key():
    settings = get_settings()
    old = settings.vault_key
    sealed = vault.seal("pem")
    new = Fernet.generate_key().decode()
    settings.vault_key = f"{new},{old}"  # newest first
    # Old rows still open during the rotation window.
    assert vault.open_sealed(sealed.ciphertext, sealed.wrapped_dek, sealed.key_version) == "pem"
    wrapped, version = vault.rewrap(sealed.wrapped_dek, sealed.key_version)
    assert version == "local:" + hashlib.sha256(new.encode()).hexdigest()[:12]
    settings.vault_key = new  # drop the old key
    assert vault.open_sealed(sealed.ciphertext, wrapped, version) == "pem"
    with pytest.raises(VaultError):  # the un-rotated wrap is now dead
        vault.open_sealed(sealed.ciphertext, sealed.wrapped_dek, sealed.key_version)


def test_legacy_blob_decrypts_with_any_listed_key():
    settings = get_settings()
    old = settings.vault_key
    legacy = Fernet(old).encrypt(b"pem")
    settings.vault_key = f"{Fernet.generate_key().decode()},{old}"
    assert vault.open_sealed(legacy, None, vault.LEGACY_VERSION) == "pem"


def test_missing_vault_key_refuses():
    get_settings().vault_key = ""
    with pytest.raises(VaultError, match="VAULT_KEY"):
        vault.seal("x")


def test_unknown_key_wrapper_setting_refuses():
    settings = get_settings()
    original = settings.key_wrapper
    settings.key_wrapper = "plaintext"
    try:
        with pytest.raises(VaultError, match="KEY_WRAPPER"):
            vault.seal("x")
    finally:
        settings.key_wrapper = original


# ---- AWS KMS via Stubber ----------------------------------------------------------

RAW_DEK = bytes(range(32))  # what KMS would hand back as Plaintext
WRAPPED = b"kms-wrapped-dek-blob"
KMS_ARN_2 = "arn:aws:kms:us-east-1:111122223333:key/5678efgh-56ef-78gh-90ij-567890abcdef"


def _describe(stub, key_id, arn):
    stub.add_response(
        "describe_key",
        {"KeyMetadata": {"KeyId": arn.rsplit("/", 1)[1], "Arn": arn}},
        {"KeyId": key_id},
    )


def test_kms_wrapper_generate_and_decrypt():
    client = _kms_client()
    stub = Stubber(client)
    stub.add_response(
        "generate_data_key",
        {"CiphertextBlob": WRAPPED, "Plaintext": RAW_DEK, "KeyId": KMS_ARN},
        {"KeyId": KMS_KEY, "KeySpec": "AES_256"},
    )
    _describe(stub, KMS_KEY, KMS_ARN)
    stub.add_response(
        "decrypt",
        {"Plaintext": RAW_DEK, "KeyId": KMS_ARN, "EncryptionAlgorithm": "SYMMETRIC_DEFAULT"},
        {"CiphertextBlob": WRAPPED, "KeyId": KMS_ARN},
    )
    with stub:
        wrapper = AwsKmsKeyWrapper(KMS_KEY, client=client)
        sealed = vault.seal("kraken-secret", wrapper=wrapper)
        assert sealed.wrapped_dek == WRAPPED
        # The alias is resolved: the version names the key that wrapped the DEK.
        assert sealed.key_version == f"aws_kms:{KMS_ARN}"
        # The Fernet DEK is urlsafe-base64 of the 32 KMS bytes.
        assert Fernet(base64.urlsafe_b64encode(RAW_DEK)).decrypt(sealed.ciphertext) == (
            b"kraken-secret"
        )
        vault.use_key_wrapper(wrapper)
        assert vault.open_sealed(sealed.ciphertext, WRAPPED, sealed.key_version) == (
            "kraken-secret"
        )
        stub.assert_no_pending_responses()


def test_kms_rewrap_uses_reencrypt_and_never_sees_plaintext():
    client = _kms_client()
    stub = Stubber(client)
    new_key = "alias/koine-vault-2026"
    stub.add_response(
        "re_encrypt",
        {"CiphertextBlob": b"rewrapped", "SourceKeyId": KMS_ARN, "KeyId": KMS_ARN_2},
        {"CiphertextBlob": WRAPPED, "SourceKeyId": KMS_ARN, "DestinationKeyId": new_key},
    )
    _describe(stub, new_key, KMS_ARN_2)
    with stub:
        vault.use_key_wrapper(AwsKmsKeyWrapper(new_key, client=client))
        wrapped, version = vault.rewrap(WRAPPED, f"aws_kms:{KMS_ARN}")
        assert (wrapped, version) == (b"rewrapped", f"aws_kms:{KMS_ARN_2}")
        stub.assert_no_pending_responses()


def test_local_to_kms_migration_rewraps_across_wrappers():
    local_sealed = vault.seal("pem")  # settings wrapper: local
    dek = LocalKeyWrapper.from_settings().unwrap(local_sealed.wrapped_dek, "local:x")
    client = _kms_client()
    stub = Stubber(client)
    stub.add_response(
        "encrypt",
        {"CiphertextBlob": b"kms-wrap-of-local-dek", "KeyId": KMS_ARN},
        {"KeyId": KMS_KEY, "Plaintext": base64.urlsafe_b64decode(dek)},
    )
    _describe(stub, KMS_KEY, KMS_ARN)
    stub.add_response(
        "decrypt",
        {
            "Plaintext": base64.urlsafe_b64decode(dek),
            "KeyId": KMS_ARN,
            "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
        },
        {"CiphertextBlob": b"kms-wrap-of-local-dek", "KeyId": KMS_ARN},
    )
    with stub:
        vault.use_key_wrapper(AwsKmsKeyWrapper(KMS_KEY, client=client))
        wrapped, version = vault.rewrap(local_sealed.wrapped_dek, local_sealed.key_version)
        assert version == f"aws_kms:{KMS_ARN}"
        # Same record ciphertext, now opened through KMS.
        assert vault.open_sealed(local_sealed.ciphertext, wrapped, version) == "pem"


def test_repointed_alias_keeps_old_rows_readable_and_due_for_rotation():
    """Rows sealed while alias/koine-vault named key A must still decrypt, with
    KeyId = key A, after the alias is re-pointed to key B (Decrypt with the
    alias would now fail with IncorrectKeyException), and must count as due
    for rotation."""
    client = _kms_client()
    stub = Stubber(client)
    stub.add_response(
        "generate_data_key",
        {"CiphertextBlob": WRAPPED, "Plaintext": RAW_DEK, "KeyId": KMS_ARN},
        {"KeyId": KMS_KEY, "KeySpec": "AES_256"},
    )
    _describe(stub, KMS_KEY, KMS_ARN)  # before: alias -> key A
    _describe(stub, KMS_KEY, KMS_ARN_2)  # after the re-point (new process): key B
    stub.add_response(
        "decrypt",
        {"Plaintext": RAW_DEK, "KeyId": KMS_ARN, "EncryptionAlgorithm": "SYMMETRIC_DEFAULT"},
        {"CiphertextBlob": WRAPPED, "KeyId": KMS_ARN},
    )
    with stub:
        sealed = vault.seal("pem", wrapper=AwsKmsKeyWrapper(KMS_KEY, client=client))
        after = AwsKmsKeyWrapper(KMS_KEY, client=client)
        assert after.version == f"aws_kms:{KMS_ARN_2}" != sealed.key_version
        vault.use_key_wrapper(after)
        assert vault.open_sealed(sealed.ciphertext, WRAPPED, sealed.key_version) == "pem"
        stub.assert_no_pending_responses()


def test_kms_errors_surface_as_vault_error_without_detail():
    client = _kms_client()
    stub = Stubber(client)
    stub.add_client_error("decrypt", service_error_code="AccessDeniedException")
    with stub, pytest.raises(VaultError, match="ClientError"):
        AwsKmsKeyWrapper(KMS_KEY, client=client).unwrap(WRAPPED, f"aws_kms:{KMS_KEY}")


def test_self_test_round_trips_current_wrapper():
    vault.self_test()  # local wrapper from the pinned test VAULT_KEY
