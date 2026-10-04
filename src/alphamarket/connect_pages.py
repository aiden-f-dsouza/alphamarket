"""Connect flows beyond a pasted key (wave2, workstream w2-p8-connect):
consent screen, Hyperliquid signed claim, SnapTrade Connection Portal
start/return and account picker. Included by main.py BEFORE pages.router, so
these paths win over pages.py's catch-all /dashboard/accounts/{id}/{action}.

Routes (HTML, session cookie; CSRF Origin check applies to every POST):
  GET/POST /dashboard/connect/claim/{account_id}
      Prove an address-keyed account is yours. PROFILE_CODE venues show the
      bio code; SIGNED_MESSAGE venues (Hyperliquid, D21) show the exact
      claim_text(stored code) to sign. static/claim.js asks an injected
      wallet for eth_requestAccounts + personal_sign and fills the form; a
      pasted signature works without JS. venues.claim_account checks it.
  POST /dashboard/connect/{venue}/start
      Aggregator connect (SnapTrade, D20): consent checked, one aggregator
      user registered, a PENDING account created with the user secret in
      venue_credentials, then on to the picker, which links to the portal.
  GET  /dashboard/connect/snaptrade/{account_id}
      The return page and account picker: portal link (connectionType=read)
      while nothing is linked, then the linked brokerage accounts. Each load
      calls SnapTrade twice, so it has its own inbound limit
      (ratelimit "snaptrade_picker_user", 429 rendered as this page) and its
      calls spend the outbound buckets like the worker's, failing fast: when
      the limiter would make the request wait more than a couple of seconds,
      the circuit is open or SnapTrade answers 429, the page says it is busy
      (503, Retry-After) and nothing more is sent.
  POST /dashboard/connect/snaptrade/{account_id}
      Finish: venues.connect_account(pending=account) with the chosen
      account_ref and a strategy name; the account becomes ACTIVE.

Why the portal is a link and not a redirect from the form POST: the CSP has
`form-action 'self'`, and Chrome applies form-action to the redirects that
follow a form submission, so a 303 from the start POST to the portal origin
would be blocked. The start POST redirects to our own picker page instead,
and the portal opens from an ordinary link there.

Every path keeps the partner-approval gate (venues.ensure_venue_open) and
the connect gate (TOTP, verified email, fresh step-up; mfa.connect_block).
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from alphamarket import credentials, mfa, ratelimit
from alphamarket.config import get_settings
from alphamarket.connectors.base import CredentialKind, OwnershipProof
from alphamarket.connectors.registry import (
    PROFILES,
    aggregator_list_accounts,
    aggregator_portal_url,
    build_connector,
    get_profile,
)
from alphamarket.db import get_db
from alphamarket.models import User, VenueAccount, VenueAccountStatus
from alphamarket.pages import _connect_ctx, _render, _user
from alphamarket.venue_limits import VenueBusy, call_venue, credential_ref, get_web_limiter
from alphamarket.venues import (
    ConnectIn,
    _owned_account,
    begin_aggregator_connect,
    claim_account,
    connect_account,
    venue_busy_message,
    venue_closed_reason,
)

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False, tags=["connect"])


def _login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# ---- ownership claim ---------------------------------------------------------------


def _claim_ctx(account: VenueAccount) -> dict:
    profile = get_profile(account.venue)
    message = None
    if profile.ownership is OwnershipProof.SIGNED_MESSAGE and account.claim_code:
        connector = build_connector(account.venue, account.venue_env, account.key_id, None)
        message = connector.claim_text(account.claim_code)  # from the STORED code
    return {"account": account, "profile": profile, "claim_message": message}


def _claim_page(
    request: Request, user: User, account: VenueAccount, error: str | None, status: int = 200
) -> HTMLResponse:
    resp = _render(request, "claim.html", user=user, error=error, **_claim_ctx(account))
    resp.status_code = status
    return resp


@router.get("/dashboard/connect/claim/{account_id:int}", response_class=HTMLResponse)
def claim_page(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login()
    account = _owned_account(db, account_id, user)
    if account.claimed or account.claim_code is None:
        return RedirectResponse("/dashboard", status_code=303)
    return _claim_page(request, user, account, None)


@router.post("/dashboard/connect/claim/{account_id:int}")
def claim_submit(
    request: Request,
    account_id: int,
    signature: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return _login()
    account = _owned_account(db, account_id, user)
    try:
        ratelimit.check(request, "venues_user", user.id)
        claim_account(
            db, user, account.id, signature=signature, limiter=get_web_limiter(request.app)
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            raise
        resp = _claim_page(request, user, account, str(exc.detail), status=exc.status_code)
        if exc.status_code == 503 and exc.headers:  # busy: the account is still unclaimed
            resp.headers["Retry-After"] = exc.headers.get("Retry-After", "60")
        return resp
    return RedirectResponse("/dashboard", status_code=303)


# ---- aggregator (SnapTrade) --------------------------------------------------------


def _picker_url(account: VenueAccount) -> str:
    return f"/dashboard/connect/snaptrade/{account.id}"


def _picker_ctx(
    request: Request, db: Session, user: User, account: VenueAccount, *, fetch: bool = True
) -> dict:
    """Linked accounts and a fresh portal link (SnapTrade links last 5
    minutes, so one is made per page view). Opening the user secret is an
    audited decrypt like any other. Both SnapTrade calls go through the
    outbound limiter and fail fast (`busy` is set and nothing more is sent
    when it says wait). `fetch=False` renders the page without touching
    SnapTrade or the vault (the inbound 429 page): `hold` hides the retry
    link."""
    profile = get_profile(account.venue)
    ctx: dict = {
        "account": account,
        "profile": profile,
        "gate": mfa.connect_block(request, db, user),
        "closed_reason": venue_closed_reason(account.venue),
        "accounts": [],
        "portal_url": None,
        "load_error": None,
        "busy": False,
        "hold": not fetch,
        "picker_url": _picker_url(account),
    }
    if not fetch:
        return ctx
    try:
        opened = credentials.open_credential(db, account, purpose="aggregator_accounts")
    except credentials.CredentialUnavailable:
        ctx["load_error"] = "This connection expired. Start again from the connect page."
        return ctx
    limiter = get_web_limiter(request.app)
    cred_id = credential_ref(account.venue, account.key_id)
    try:
        ctx["accounts"] = call_venue(
            limiter,
            account.venue,
            cred_id,
            lambda throttle: aggregator_list_accounts(
                account.venue, account.key_id, opened.secret, throttle=throttle
            ),
        )
    except VenueBusy as exc:
        return _picker_busy(ctx, profile.display_name, exc)
    except Exception as exc:  # noqa: BLE001 - shown as a retry message, never echoed
        logger.warning("%s account list failed: %s", account.venue, type(exc).__name__)
        ctx["load_error"] = f"Could not read your {profile.display_name} accounts; try again."
    if ctx["closed_reason"] is None:
        redirect = get_settings().public_base_url.rstrip("/") + _picker_url(account)
        try:
            ctx["portal_url"] = call_venue(
                limiter,
                account.venue,
                cred_id,
                lambda throttle: aggregator_portal_url(
                    account.venue,
                    account.key_id,
                    opened.secret,
                    redirect=redirect,
                    throttle=throttle,
                ),
            )
        except VenueBusy as exc:
            return _picker_busy(ctx, profile.display_name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s portal link failed: %s", account.venue, type(exc).__name__)
    return ctx


def _picker_busy(ctx: dict, display_name: str, exc: VenueBusy) -> dict:
    logger.warning("%s picker call refused: %s", exc.venue, exc.reason)
    ctx["busy"] = True
    ctx["retry_after"] = max(1, round(exc.retry_after_seconds))
    ctx["load_error"] = venue_busy_message(display_name)
    return ctx


def _picker_page(
    request: Request,
    db: Session,
    user: User,
    account: VenueAccount,
    error: str | None,
    status: int = 200,
    *,
    fetch: bool = True,
) -> HTMLResponse:
    ctx = _picker_ctx(request, db, user, account, fetch=fetch)
    resp = _render(request, "snaptrade_accounts.html", user=user, error=error, **ctx)
    if ctx["busy"]:  # SnapTrade's side: 503 unless a form error already set a status
        resp.headers["Retry-After"] = str(ctx["retry_after"])
        status = 503 if status == 200 else status
    resp.status_code = status
    return resp


def _pending_or_none(db: Session, account_id: int, user: User) -> VenueAccount | None:
    account = _owned_account(db, account_id, user)
    if get_profile(account.venue).credential is not CredentialKind.AGGREGATOR:
        raise HTTPException(status_code=404)
    return account if account.status == VenueAccountStatus.PENDING else None


@router.post("/dashboard/connect/{venue}/start")
def aggregator_start(
    request: Request,
    venue: str,
    consent_sha256: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return _login()
    if venue not in PROFILES:
        raise HTTPException(status_code=404, detail="Unknown venue")
    ctx = _connect_ctx(venue)
    gate = mfa.connect_block(request, db, user)
    if gate is not None:
        resp = _render(request, "connect.html", user=user, error=gate[1], gate=gate, **ctx)
        resp.status_code = 403
        return resp
    try:
        ratelimit.check(request, "venues_user", user.id)
        account = begin_aggregator_connect(db, user, venue, consent_sha256, request)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise
        resp = _render(request, "connect.html", user=user, error=exc.detail, gate=None, **ctx)
        resp.status_code = exc.status_code
        if exc.status_code == 503 and exc.headers:  # "busy": say when to come back
            resp.headers["Retry-After"] = exc.headers.get("Retry-After", "60")
        return resp
    return RedirectResponse(_picker_url(account), status_code=303)


@router.get("/dashboard/connect/snaptrade/{account_id:int}", response_class=HTMLResponse)
def aggregator_accounts(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login()
    account = _pending_or_none(db, account_id, user)
    if account is None:  # finished (or abandoned): nothing left to pick
        return RedirectResponse("/dashboard", status_code=303)
    try:
        ratelimit.check(request, "snaptrade_picker_user", user.id)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        # A page you navigate to, so the 429 is this page with an error, not
        # JSON; neither SnapTrade nor the vault is touched.
        resp = _picker_page(
            request,
            db,
            user,
            account,
            "You have reloaded this page too many times. Wait a few minutes and try again.",
            status=429,
            fetch=False,
        )
        resp.headers["Retry-After"] = (exc.headers or {}).get("Retry-After", "60")
        return resp
    return _picker_page(request, db, user, account, None)


@router.post("/dashboard/connect/snaptrade/{account_id:int}")
def aggregator_finish(
    request: Request,
    account_id: int,
    account_ref: str = Form(""),
    strategy_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return _login()
    account = _pending_or_none(db, account_id, user)
    if account is None:
        return RedirectResponse("/dashboard", status_code=303)
    gate = mfa.connect_block(request, db, user)
    if gate is not None:
        return _picker_page(request, db, user, account, gate[1], status=403)
    if not account_ref.strip():
        return _picker_page(request, db, user, account, "Pick an account to connect.", 422)
    try:
        ratelimit.check(request, "venues_user", user.id)
        payload = ConnectIn(
            venue=account.venue,
            key_id=account.key_id,
            strategy_name=strategy_name.strip(),
            account_ref=account_ref.strip(),
        )
        connect_account(db, user, payload, request=request, pending=account)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
        error = "Check these fields: " + ", ".join(fields).replace("_", " ") + "."
        return _picker_page(request, db, user, account, error, status=422)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise
        db.rollback()
        if account.status != VenueAccountStatus.PENDING:  # finished by a concurrent submit
            return RedirectResponse("/dashboard", status_code=303)
        resp = _picker_page(request, db, user, account, str(exc.detail), status=exc.status_code)
        if exc.status_code == 503 and exc.headers:
            # SnapTrade is busy (the outbound limiter refused the finish's calls, or
            # it answered 429): nothing was written, the account is still PENDING
            # and this page, re-rendered with the picker, is the retry.
            resp.headers["Retry-After"] = exc.headers.get("Retry-After", "60")
        return resp
    return RedirectResponse("/dashboard", status_code=303)
