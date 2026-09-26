"""Breached-password check: HIBP Pwned Passwords range API (k-anonymity).

Only the first 5 hex characters of the password's SHA-1 leave the server
(`GET /range/{prefix}`); the API answers with every known suffix sharing
that prefix and we look for ours locally. `Add-Padding: true` asks for decoy
rows with count 0, so the response size does not hint at the prefix; those
rows never count as a match.

Used on signup and password reset. If HIBP is unreachable the password is
allowed and a warning is logged: an outage must not lock people out.
Disabled unless HIBP_ENABLED is true (preflight requires it in prod) so no
test or dev run ever calls the network (invariant 6).
"""

import hashlib
import logging

import httpx
from fastapi import HTTPException

from alphamarket.config import get_settings

logger = logging.getLogger(__name__)

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/"
BREACHED_DETAIL = (
    "This password appears in a known data breach, so it is easy to guess. Choose a different one."
)


def sha1_split(password: str) -> tuple[str, str]:
    """(5-char prefix sent to HIBP, 35-char suffix kept here), uppercase hex."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 - HIBP format
    return digest[:5], digest[5:]


def breach_count(password: str, timeout: float = 3.0) -> int | None:
    """Times the password appears in HIBP; 0 if never; None if unreachable."""
    prefix, suffix = sha1_split(password)
    try:
        resp = httpx.get(
            HIBP_RANGE_URL + prefix,
            headers={"Add-Padding": "true", "User-Agent": "Koine-password-check"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("HIBP range check unavailable (%s); allowing password", type(exc).__name__)
        return None
    for line in resp.text.splitlines():
        found, _, count = line.strip().partition(":")
        if found.upper() == suffix:
            try:
                return int(count)
            except ValueError:
                return 0
    return 0


def ensure_not_breached(password: str) -> None:
    """Raise 422 if HIBP knows the password. No-op when the check is off."""
    if not get_settings().hibp_enabled:
        return
    count = breach_count(password)
    if count:  # None (unreachable) and 0 (unknown or padding) both pass
        raise HTTPException(status_code=422, detail=BREACHED_DETAIL)
