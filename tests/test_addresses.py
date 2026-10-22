"""connectors/addresses.py (docs/onchain_perps_plan.md S1): address kinds,
normalization, base58, EIP-55 and ed25519 ownership proofs. Pure: no
network, no database. Keys are generated in the test, never a real wallet."""

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from alphamarket.connectors import addresses
from alphamarket.connectors.addresses import (
    HINT,
    AddressKind,
    InvalidAddress,
    b58decode,
    b58encode,
    evm_checksum,
    is_valid,
    normalize,
    short,
    verify_ed25519,
)

EVM, SOL = AddressKind.EVM, AddressKind.SOLANA
# Public program ids (not anyone's wallet): 32-byte keys in base58.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"  # 32 zero bytes


# ---- EVM -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "checksummed",
    [
        # EIP-55's own test vectors (mixed case, all caps, all lower).
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
        "0x52908400098527886E0F7030069857D2E4169EE7",
        "0x8617E340B3D01FA5F11F306F4090FD50E238070D",
        "0xde709f2102306220921060314715629080e2fb77",
        "0x27b1fdb04752bbc536007a920d24acb045561c26",
    ],
)
def test_eip55_vectors(checksummed):
    stored = normalize(EVM, checksummed)
    assert stored == checksummed.lower()
    assert evm_checksum(stored) == checksummed
    assert evm_checksum(checksummed.upper().replace("0X", "0x")) == checksummed


def test_evm_is_stored_lowercased_exactly_as_before():
    # The watchlist and connect paths stored strip().lower() before address
    # kinds existed; the normalizer keeps those rows byte-identical.
    raw = "  0xABCdef0000000000000000000000000000000001 \n"
    assert normalize(EVM, raw) == "0xabcdef0000000000000000000000000000000001"
    assert is_valid(EVM, "0xabcdef0000000000000000000000000000000001")
    assert not is_valid(EVM, "0xABCdef0000000000000000000000000000000001")  # not normalized


@pytest.mark.parametrize(
    "bad",
    ["", None, "0x123", "0x" + "g" * 40, "1x" + "a" * 40, "0x" + "a" * 41, TOKEN_PROGRAM],
)
def test_evm_rejections_carry_the_hint(bad):
    with pytest.raises(InvalidAddress) as exc:
        normalize(EVM, bad)
    assert str(exc.value) == HINT[EVM] == "Enter a 0x address (42 characters)"


# ---- base58 / Solana ----------------------------------------------------------------------


def test_base58_round_trip_known_keys():
    assert b58decode(SYSTEM_PROGRAM) == bytes(32)
    assert b58encode(bytes(32)) == SYSTEM_PROGRAM
    token = b58decode(TOKEN_PROGRAM)
    assert len(token) == 32
    assert b58encode(token) == TOKEN_PROGRAM
    # Leading zero bytes are leading '1's, both ways.
    assert b58encode(b"\x00\x00\x01") == "112"
    assert b58decode("112") == b"\x00\x00\x01"
    # 58 = "21" in base58 (2 x 58 + 0 -> digits 1, 0 -> "2", "1").
    assert b58decode("21") == bytes([58])


def test_base58_round_trip_on_a_generated_key():
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    address = b58encode(raw)
    assert b58decode(address) == raw
    assert normalize(SOL, address) == address


def test_solana_case_is_preserved():
    assert normalize(SOL, f"  {TOKEN_PROGRAM}\t") == TOKEN_PROGRAM
    # Changing the case is a different key, never folded into this one.
    lowered = TOKEN_PROGRAM.lower()
    assert normalize(SOL, lowered) == lowered
    assert b58decode(lowered) != b58decode(TOKEN_PROGRAM)
    assert is_valid(SOL, TOKEN_PROGRAM) and not is_valid(SOL, f" {TOKEN_PROGRAM}")


def _b58_of(n_bytes: int) -> str:
    return b58encode(b"\x01" + b"\x07" * (n_bytes - 1))


def test_solana_rejects_31_and_33_byte_keys_and_bad_characters():
    assert normalize(SOL, _b58_of(32)) == _b58_of(32)
    for wrong in (_b58_of(31), _b58_of(33)):
        with pytest.raises(InvalidAddress) as exc:
            normalize(SOL, wrong)
        assert str(exc.value) == HINT[SOL]
    for ch in "0OIl":  # not in the base58 alphabet
        bad = TOKEN_PROGRAM[:10] + ch + TOKEN_PROGRAM[11:]
        with pytest.raises(InvalidAddress):
            normalize(SOL, bad)
    with pytest.raises(ValueError):
        b58decode("abc0")
    for bad in ("", None, "0x" + "a" * 40, "1" * 31, "1" * 45):
        with pytest.raises(InvalidAddress):
            normalize(SOL, bad)


def test_short_is_kind_aware():
    assert short("0x1a13afd75418fd72dfa80da33cbca489e231caec") == "0x1a13…caec"
    assert short(TOKEN_PROGRAM) == "Toke…Q5DA"
    assert short("0x12") == "0x12"


# ---- ed25519 ownership proof ---------------------------------------------------------------


@pytest.fixture()
def wallet():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, b58encode(raw)


MESSAGE = "Koine ownership claim\nVenue: Test\nAddress: {a}\nCode: koine-optout-abc123abc123\n"


def test_ed25519_verifies_in_all_three_encodings(wallet):
    key, address = wallet
    text = MESSAGE.format(a=address)
    sig = key.sign(text.encode("utf-8"))
    for encoded in (
        b58encode(sig),
        base64.b64encode(sig).decode(),
        "0x" + sig.hex(),
        "0X" + sig.hex().upper(),
        f"  {b58encode(sig)}\n",
    ):
        assert verify_ed25519(address, text, encoded), encoded


def test_ed25519_rejects_another_message_key_or_garbage(wallet):
    key, address = wallet
    text = MESSAGE.format(a=address)
    sig = key.sign(text.encode("utf-8"))
    assert not verify_ed25519(address, text + " ", b58encode(sig))
    assert not verify_ed25519(address, text.replace("abc123", "abc124"), b58encode(sig))
    other = b58encode(
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    assert not verify_ed25519(other, text, b58encode(sig))
    for junk in ("", "0x", "0xzz", "not a signature", b58encode(sig[:63]), "0x" + sig.hex()[:-2]):
        assert not verify_ed25519(address, text, junk)
    assert not verify_ed25519("0x" + "a" * 40, text, b58encode(sig))  # not a Solana address


def test_ui_copy_covers_every_kind():
    for table in (addresses.HINT, addresses.PLACEHOLDER, addresses.SIGN_HELP):
        assert set(table) == set(AddressKind)
    assert "personal_sign / EIP-191" in addresses.SIGN_HELP[EVM]
    assert "Phantom" in addresses.SIGN_HELP[SOL]
    assert addresses.MAX_INPUT_LENGTH == 66  # 0x + 64 hex (bytes32), the widest in scope
