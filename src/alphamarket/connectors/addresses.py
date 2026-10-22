"""Public address kinds for address-keyed venues (on-chain perps scaffold,
docs/onchain_perps_plan.md S1). Pure: no I/O, no database.

A venue whose credential is `CredentialKind.ADDRESS` names its kind in
`VenueProfile.address_kind`; every place that turns user input or venue data
into a stored address goes through `normalize(kind, raw)`, so one address has
exactly one stored spelling:

- EVM: `0x` + 40 hex, stored lowercased (byte-identical to what the watchlist
  and connect paths stored before address kinds existed).
- SOLANA: base58 of a 32-byte ed25519 public key. Base58 is case-sensitive,
  so it is stored exactly as given (only surrounding whitespace is stripped).

No column is widened: every address column allows 120 characters, and the
widest address in scope is a 66-character bytes32 hex.
"""

import base64
import binascii
import re
from contextlib import suppress
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AddressKind(StrEnum):
    EVM = "evm"  # 0x + 40 hex, stored lowercased
    SOLANA = "solana"  # base58, 32-byte ed25519 public key, case-sensitive, stored as given


class InvalidAddress(ValueError):  # noqa: N818 (a user-input error, like ValueError)
    """The input is not an address of the kind. str(exc) is user-facing copy."""


HINT: dict[AddressKind, str] = {
    AddressKind.EVM: "Enter a 0x address (42 characters)",
    AddressKind.SOLANA: "Enter a Solana address (32-44 characters, case-sensitive)",
}

PLACEHOLDER: dict[AddressKind, str] = {
    AddressKind.EVM: "0x…",
    AddressKind.SOLANA: "Solana address",
}

# HTML input attributes per kind. A form that serves several kinds (the venue
# <select> on /watch) drops the pattern and uses the widest maxlength; the
# server validates either way.
INPUT_PATTERN: dict[AddressKind, dict[str, str | int]] = {
    AddressKind.EVM: {"pattern": "0[xX][0-9a-fA-F]{40}", "maxlength": 42},
    AddressKind.SOLANA: {"pattern": "[1-9A-HJ-NP-Za-km-z]{32,44}", "maxlength": 44},
}
MAX_INPUT_LENGTH = 66  # Nado's bytes32 hex (0x + 64), the widest address in scope

SIGN_HELP: dict[AddressKind, str] = {
    AddressKind.EVM: (
        "No browser wallet? Sign the exact message above with your wallet's \"sign "
        'message" tool (personal_sign / EIP-191) and paste the 0x signature here.'
    ),
    AddressKind.SOLANA: (
        "Use Sign Message in Phantom or Solflare (or any wallet that signs the raw "
        "message bytes) with the wallet that holds the address, then paste the "
        "signature here (base58, base64 or 0x hex)."
    ),
}

_EVM_RE = re.compile(r"^0x[0-9a-f]{40}$")
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}
SOLANA_KEY_BYTES = 32
SOLANA_MIN_LEN, SOLANA_MAX_LEN = 32, 44
ED25519_SIGNATURE_BYTES = 64


# ---- base58 (Bitcoin alphabet, as Solana uses it) ----------------------------------


def b58decode(value: str) -> bytes:
    """Decode a base58 string. Each leading '1' is one leading zero byte.
    Raises ValueError on a character outside the alphabet (0, O, I, l)."""
    n = 0
    for ch in value:
        try:
            n = n * 58 + _B58_INDEX[ch]
        except KeyError:
            raise ValueError(f"not a base58 character: {ch!r}") from None
    zeros = len(value) - len(value.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * zeros + body


def b58encode(data: bytes) -> str:
    """Encode bytes as base58 (tests and synthetic demo addresses)."""
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58_ALPHABET[r])
    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(out))


# ---- normalization ---------------------------------------------------------------------


def normalize(kind: AddressKind, raw: str | None) -> str:
    """The stored form of an address of `kind`, or InvalidAddress (its message
    is the kind's HINT, safe to show the user)."""
    text = (raw or "").strip()
    if kind is AddressKind.EVM:
        address = text.lower()
        if not _EVM_RE.match(address):
            raise InvalidAddress(HINT[kind])
        return address
    if kind is AddressKind.SOLANA:
        if not SOLANA_MIN_LEN <= len(text) <= SOLANA_MAX_LEN:
            raise InvalidAddress(HINT[kind])
        try:
            key = b58decode(text)
        except ValueError:
            raise InvalidAddress(HINT[kind]) from None
        if len(key) != SOLANA_KEY_BYTES:
            raise InvalidAddress(HINT[kind])
        return text
    raise InvalidAddress(f"Unsupported address kind: {kind}")


def is_valid(kind: AddressKind, address: str) -> bool:
    """True when `address` is already in its stored (normalized) form."""
    try:
        return normalize(kind, address) == address
    except InvalidAddress:
        return False


def short(address: str) -> str:
    """Enough to tell addresses apart on a page: 0x12ab…89cd (EVM keeps the
    0x and four hex digits) or 9GN1…AxUh (base58)."""
    head = 6 if address.startswith("0x") else 4
    return f"{address[:head]}…{address[-4:]}" if len(address) > head + 6 else address


def evm_checksum(address: str) -> str:
    """EIP-55 mixed-case form (some venue APIs only match it, e.g. GMX's
    GraphQL). Stored addresses stay lowercased; this is for outbound queries."""
    from eth_utils import to_checksum_address

    return to_checksum_address(normalize(AddressKind.EVM, address))


# ---- ed25519 ownership proof (Solana wallets) ---------------------------------------


def _signature_candidates(signature: str) -> list[bytes]:
    """Every 64-byte reading of `signature` as 0x-hex, base58 or base64.
    Trying each is safe: only a real signature verifies under any of them."""
    text = signature.strip()
    out: list[bytes] = []
    if text[:2] in ("0x", "0X"):
        with suppress(ValueError):
            out.append(bytes.fromhex(text[2:]))
        return [s for s in out if len(s) == ED25519_SIGNATURE_BYTES]
    with suppress(ValueError):
        out.append(b58decode(text))
    with suppress(binascii.Error, ValueError):
        out.append(base64.b64decode(text, validate=True))
    return [s for s in out if len(s) == ED25519_SIGNATURE_BYTES]


def verify_ed25519(address: str, message: str, signature: str) -> bool:
    """True when `signature` (base58, base64 or 0x-hex of 64 bytes) is the
    ed25519 signature of the exact UTF-8 bytes of `message` by the key whose
    base58 encoding is `address` (Phantom/Solflare signMessage signs the raw
    bytes, no prefix). Never raises on bad input: False."""
    try:
        key = Ed25519PublicKey.from_public_bytes(b58decode(normalize(AddressKind.SOLANA, address)))
    except (InvalidAddress, ValueError):
        return False
    data = message.encode("utf-8")
    for candidate in _signature_candidates(signature or ""):
        try:
            key.verify(candidate, data)
            return True
        except InvalidSignature:
            continue
    return False
