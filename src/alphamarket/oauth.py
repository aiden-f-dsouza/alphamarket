"""OAuth2 authorization-code + PKCE client for venues that connect by OAuth
(Alpaca first; Webull Connect and Schwab later).

Flow:
  GET /oauth/{venue}/start     (logged in) makes a random `state` and a PKCE
      verifier, keeps both in a short-lived itsdangerous-signed cookie bound
      to the user, and redirects to the venue's authorize URL with the state,
      the S256 code_challenge and the provider's scopes (Alpaca: none).
  GET /oauth/{venue}/callback  checks the state against the cookie, exchanges
      the code (backend call, client secret never leaves the server), refuses
      a grant that carries a write scope, asks the venue which account the
      token reads, and creates the venue account through
      venues.connect_account. The token JSON {access_token, refresh_token,
      expires_at, scope, token_type} is the account secret, encrypted by the
      vault like every other credential.

Tokens are never logged, never put in a URL Koine controls, never returned.
Refresh (with jitter before expiry) and revoke-on-disconnect are here too;
Alpaca's tokens document neither an expiry nor a revoke endpoint, so for
Alpaca both are no-ops and disconnect tells the user where to revoke.
"""

import base64
import hashlib
import hmac
import json
import logging
import random
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from alphamarket.auth import current_user
from alphamarket.config import get_settings
from alphamarket.db import get_db
from alphamarket.mfa import require_connect_ready
from alphamarket.models import User
from alphamarket.ratelimit import rate_limit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/oauth", tags=["oauth"])

STATE_MAX_AGE_SECONDS = 600
REFRESH_MARGIN = timedelta(minutes=5)
REFRESH_JITTER_SECONDS = 120


@dataclass(frozen=True)
class OAuthProvider:
    """One venue's OAuth endpoints and Koine's client registration there.
    Built per request from settings (registry.get_oauth_provider)."""

    venue: str
    authorize_url: str
    token_url: str
    revoke_url: str | None
    client_id: str
    client_secret: str
    scopes: tuple[str, ...] = ()  # requested; () = the venue's read-only default
    # A grant containing any of these is refused (and revoked where possible).
    forbidden_scopes: frozenset[str] = frozenset()
    extra_authorize_params: tuple[tuple[str, str], ...] = ()  # e.g. (("env", "paper"),)
    pkce: bool = True
    revoke_help: str = ""  # where the user revokes by hand when revoke_url is None
    timeout_seconds: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class OAuthError(RuntimeError):
    """Token endpoint said no. Carries the OAuth error code only, never tokens."""


# ---- PKCE (RFC 7636) ---------------------------------------------------------------


def new_code_verifier() -> str:
    """43-128 chars from the unreserved set; 64 url-safe chars here."""
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """S256: BASE64URL(SHA256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(provider: OAuthProvider, redirect_uri: str, state: str, verifier: str) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if provider.scopes:
        params["scope"] = " ".join(provider.scopes)
    if provider.pkce:
        params["code_challenge"] = code_challenge(verifier)
        params["code_challenge_method"] = "S256"
    params.update(dict(provider.extra_authorize_params))
    return f"{provider.authorize_url}?{urlencode(params)}"


# ---- signed state cookie -----------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="koine-oauth-state")


def cookie_name(venue: str) -> str:
    return f"koine_oauth_{venue}"


def dump_state(data: dict[str, Any]) -> str:
    return _serializer().dumps(data)


def load_state(raw: str | None, max_age: int = STATE_MAX_AGE_SECONDS) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=max_age)
    except BadSignature:  # includes SignatureExpired
        return None
    return data if isinstance(data, dict) else None


# ---- token endpoint ----------------------------------------------------------------


def _token_request(
    provider: OAuthProvider, form: dict[str, str], client: httpx.Client | None
) -> dict[str, Any]:
    http = client or httpx.Client(timeout=provider.timeout_seconds)
    resp = http.post(
        provider.token_url,
        data={**form, "client_id": provider.client_id, "client_secret": provider.client_secret},
        headers={"Accept": "application/json"},
    )
    if resp.status_code >= 400:
        try:
            code = str(resp.json().get("error") or resp.status_code)
        except ValueError:
            code = str(resp.status_code)
        raise OAuthError(f"{provider.venue} token endpoint refused: {code}"[:200])
    body = resp.json()
    if not isinstance(body, dict) or not body.get("access_token"):
        raise OAuthError(f"{provider.venue} token endpoint returned no access_token")
    return body


def token_record(body: dict[str, Any], now: datetime, previous: dict | None = None) -> dict:
    """What the vault stores. expires_at is absolute UTC so refresh timing
    does not depend on when the record is read.

    `scope` is None when the venue did not say what it granted: "unknown" is
    not "nothing", and the read-only proof must not read it as an empty grant.
    A refresh that omits it keeps the original grant's (RFC 6749 6)."""
    if body.get("scope") is not None:
        scope: str | None = str(body["scope"])
    else:
        scope = (previous or {}).get("scope")
    record: dict[str, Any] = {
        "access_token": str(body["access_token"]),
        "token_type": str(body.get("token_type") or "bearer"),
        "scope": scope,
    }
    refresh = body.get("refresh_token") or (previous or {}).get("refresh_token")
    if refresh:
        record["refresh_token"] = str(refresh)
    expires_in = body.get("expires_in")
    record["expires_at"] = (
        (now + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
    )
    return record


def granted_scopes(record: dict[str, Any]) -> set[str]:
    return set(str(record.get("scope") or "").split())


def exchange_code(
    provider: OAuthProvider,
    code: str,
    redirect_uri: str,
    verifier: str,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if provider.pkce:
        form["code_verifier"] = verifier
    body = _token_request(provider, form, client)
    return token_record(body, now or datetime.now(UTC))


def refresh_due(record: dict[str, Any], now: datetime, jitter_seconds: float | None = None) -> bool:
    """True when the token expires within REFRESH_MARGIN plus a random jitter
    (0-120 s), so accounts sharing an expiry do not refresh in one burst.
    Tokens without expires_at (Alpaca) or without a refresh token never are."""
    expires_at = record.get("expires_at")
    if not expires_at or not record.get("refresh_token"):
        return False
    jitter = random.uniform(0, REFRESH_JITTER_SECONDS) if jitter_seconds is None else jitter_seconds
    deadline = datetime.fromisoformat(expires_at) - REFRESH_MARGIN - timedelta(seconds=jitter)
    return now >= deadline


def refresh_token(
    provider: OAuthProvider,
    record: dict[str, Any],
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    body = _token_request(
        provider,
        {"grant_type": "refresh_token", "refresh_token": str(record["refresh_token"])},
        client,
    )
    fresh = token_record(body, now or datetime.now(UTC), previous=record)
    if granted_scopes(fresh) & provider.forbidden_scopes:
        raise OAuthError(f"{provider.venue} refresh granted a write scope")
    return fresh


def ensure_fresh(
    venue: str, secret: str, client: httpx.Client | None = None, now: datetime | None = None
) -> str | None:
    """For the worker/vault before building a connector: the new secret JSON
    if the stored token was refreshed (store it), None if nothing changed."""
    from alphamarket.connectors.registry import get_oauth_provider

    provider = get_oauth_provider(venue)
    if provider is None or not secret.strip().startswith("{"):
        return None
    record = json.loads(secret)
    now = now or datetime.now(UTC)
    if not refresh_due(record, now):
        return None
    return json.dumps(refresh_token(provider, record, client, now))


def revoke(provider: OAuthProvider, secret: str, client: httpx.Client | None = None) -> bool:
    """RFC 7009 revoke of the refresh token (or access token). True if the
    venue confirmed, False if it has no revoke endpoint or refused. Never
    raises: disconnect must erase the credential regardless."""
    if not provider.revoke_url:
        return False
    try:
        record = json.loads(secret) if secret.strip().startswith("{") else {"access_token": secret}
        token = record.get("refresh_token") or record["access_token"]
        hint = "refresh_token" if record.get("refresh_token") else "access_token"
        http = client or httpx.Client(timeout=provider.timeout_seconds)
        resp = http.post(
            provider.revoke_url,
            data={
                "token": token,
                "token_type_hint": hint,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            },
        )
        return resp.is_success
    except Exception as exc:  # noqa: BLE001 - disconnect must not fail on revoke
        logger.warning("%s token revoke failed: %s", provider.venue, type(exc).__name__)
        return False


def revoke_quietly(venue: str, secret: str) -> bool:
    """venues.disconnect hook."""
    from alphamarket.connectors.registry import get_oauth_provider

    provider = get_oauth_provider(venue)
    return revoke(provider, secret) if provider is not None else False


# ---- router ------------------------------------------------------------------------


def redirect_uri_for(venue: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/oauth/{venue}/callback"


def _provider_or_404(venue: str) -> OAuthProvider:
    from alphamarket.connectors.registry import get_oauth_provider

    provider = get_oauth_provider(venue)
    if provider is None:
        raise HTTPException(status_code=404, detail="This venue does not connect by OAuth")
    if not provider.configured:
        raise HTTPException(status_code=503, detail="OAuth for this venue is not configured")
    return provider


@router.get(
    "/{venue}/start",
    # Same gate and limit as POST /venues (P3): TOTP, verified email and a
    # fresh step-up before any grant is requested.
    dependencies=[Depends(rate_limit("venues_user", by="user"))],
)
def start(
    venue: str,
    strategy_name: str = Query(min_length=2, max_length=120),
    user: User = Depends(require_connect_ready),
) -> RedirectResponse:
    from alphamarket.venues import ensure_venue_open  # venues imports this module's users

    ensure_venue_open(venue)
    provider = _provider_or_404(venue)
    state = secrets.token_urlsafe(32)
    verifier = new_code_verifier()
    resp = RedirectResponse(
        authorize_url(provider, redirect_uri_for(venue), state, verifier), status_code=302
    )
    resp.set_cookie(
        cookie_name(venue),
        dump_state({"s": state, "v": verifier, "u": user.id, "n": strategy_name}),
        max_age=STATE_MAX_AGE_SECONDS,
        path=f"/oauth/{venue}",
        httponly=True,
        secure=get_settings().secure_cookies,
        samesite="lax",  # sent on the venue's top-level redirect back to us
    )
    return resp


@router.get("/{venue}/callback")
def callback(
    venue: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RedirectResponse:
    from alphamarket.connectors.registry import resolve_oauth_account_id
    from alphamarket.venues import ConnectIn, connect_account

    provider = _provider_or_404(venue)
    saved = load_state(request.cookies.get(cookie_name(venue)))
    if (
        saved is None
        or not state
        or not hmac.compare_digest(str(saved.get("s", "")), state)
        or saved.get("u") != user.id
    ):
        raise HTTPException(status_code=400, detail="OAuth state mismatch; start again.")
    if error or not code:
        # The user declined on the venue (error=access_denied) or it failed.
        raise HTTPException(status_code=400, detail="Authorization was not granted.")

    try:
        record = exchange_code(provider, code, redirect_uri_for(venue), str(saved["v"]))
    except (OAuthError, httpx.HTTPError):
        raise HTTPException(status_code=502, detail="Could not complete OAuth.") from None
    secret = json.dumps(record)

    if granted_scopes(record) & provider.forbidden_scopes:
        revoke(provider, secret)
        raise HTTPException(
            status_code=422,
            detail=(
                "The grant includes trading or account-write access. Koine only "
                "accepts read-only access; connect again without those permissions."
            ),
        )

    venue_env = get_settings().venue_env(venue)
    try:
        account_id = resolve_oauth_account_id(venue, venue_env, secret)
    except Exception:
        revoke(provider, secret)
        raise HTTPException(status_code=502, detail="Could not read the venue account.") from None

    try:
        connect_account(
            db,
            user,
            ConnectIn(venue=venue, key_id=account_id, secret=secret, strategy_name=saved["n"]),
            oauth_grant=True,
        )
    except HTTPException:
        # Refused (e.g. the probe showed write access): nothing was stored,
        # so the grant must not stay alive at the venue either.
        revoke(provider, secret)
        raise
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.delete_cookie(cookie_name(venue), path=f"/oauth/{venue}")
    return resp
