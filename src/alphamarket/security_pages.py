"""HTML for account security (P3): the login second step, /dashboard/security
(email, two-factor, recovery codes, sessions, password), forgot/reset
password and the email-verification landing page.

Plain forms, no client script (the CSP work keeps templates script-free).
Every action reuses the JSON-route logic in auth.py, mfa.py and account.py,
so both surfaces enforce the same limits.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from alphamarket import account, mfa, ratelimit, sessions
from alphamarket.audit import AuditAction
from alphamarket.auth import optional_user, totp_enabled
from alphamarket.connectors.registry import PROFILES
from alphamarket.db import get_db
from alphamarket.models import AuditLog, User, VenueAccount
from alphamarket.pages import _render, safe_next

router = APIRouter(include_in_schema=False)
# wave2: POST /account/email (JSON) rides on this router, which main.py includes.


def _safe_next(next_url: str | None, default: str = "/dashboard/security") -> str:
    """pages.safe_next (the one open-redirect guard), defaulting here."""
    return safe_next(next_url, default)


def _status(exc: HTTPException) -> int:
    return exc.status_code if exc.status_code in (401, 403, 409, 422, 429) else 400


# ---- login, second step -------------------------------------------------------


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, next: str = "", db: Session = Depends(get_db)):
    if sessions.pending(db, request) is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "login_2fa.html", user=None, error=None, next=safe_next(next, ""))


@router.post("/login/2fa")
def login_2fa(
    request: Request, code: str = Form(), next: str = Form(""), db: Session = Depends(get_db)
):
    next = safe_next(next, "")
    try:
        mfa.complete_login(request, db, code)
    except HTTPException as exc:
        if sessions.pending(db, request) is None:
            return RedirectResponse("/login", status_code=303)
        resp = _render(request, "login_2fa.html", user=None, error=str(exc.detail), next=next)
        resp.status_code = _status(exc)
        return resp
    return RedirectResponse(next or "/dashboard", status_code=303)


# ---- /dashboard/security --------------------------------------------------------


def _security_ctx(request: Request, db: Session, user: User, **extra) -> dict:
    current = sessions.load(db, request)
    rows = sessions.active_sessions(db, user.id)
    return {
        "user": user,
        "email_verified": user.email_verified_at is not None,
        "totp_on": totp_enabled(db, user),
        "recovery_left": mfa.remaining_recovery_codes(db, user),
        "stepped_up": sessions.has_recent_step_up(current),
        "sessions": [
            {
                "id": s.id,
                "ip": s.ip or "unknown",
                "agent": s.user_agent or "unknown device",
                "created": sessions.aware(s.created_at),
                "seen": sessions.aware(s.last_seen_at),
                "current": current is not None and s.id == current.id,
            }
            for s in rows
        ],
        "next": _safe_next(request.query_params.get("next"), ""),
        "notice": None,
        "error": None,
        "audit_rows": audit_trail(db, user),
        **extra,
    }


# ---- wave2:account-security: audit trail ---------------------------------------------

AUDIT_TRAIL_LIMIT = 50
AUDIT_TRAIL_DECRYPTS = 10  # the newest key uses, merged into the list
AUDIT_LABELS = {
    "credential.connect": "Key stored",
    "credential.rejected": "Key refused (not read-only)",
    "credential.disconnect": "Key erased",
    "credential.rotate": "Key replaced",
    "credential.decrypt": "Key used",
    "credential.refresh": "Access token refreshed",
    "scope.check": "Read-only check",
    "account.suspend": "Connection suspended",
    "aggregator.user_delete": "Brokerage link removed",
    "venue.consent": "Venue terms accepted",
    "account.email_change_request": "Email change requested",
    "account.email_change": "Email changed",
    "security.login": "Signed in",
    "security.new_login": "Sign-in from a new device",
    "security.password_changed": "Password changed",
    "security.totp_enabled": "Two-factor turned on",
    "security.totp_disabled": "Two-factor turned off",
    "security.totp_lockout": "Two-factor locked after wrong codes",
    "security.recovery_code_used": "Recovery code used",
    "security.sessions_revoked": "Signed out everywhere",
}
# The only detail fields the page shows; each is a short label, never a secret.
_PURPOSE_KEYS = ("purpose", "trigger", "reason", "outcome")


def audit_trail(db: Session, user: User, limit: int = AUDIT_TRAIL_LIMIT) -> list[dict]:
    """The user's own audit rows, newest first: what they did (actor), and
    everything done to their venue accounts, including the worker's decrypts
    (no actor, target = one of their accounts). Never another user's rows,
    and from `details` only the venue and one purpose/trigger label."""
    venues_by_id = {
        str(account_id): venue
        for account_id, venue in db.execute(
            select(VenueAccount.id, VenueAccount.venue).where(VenueAccount.user_id == user.id)
        ).all()
    }
    conditions = [AuditLog.actor_user_id == user.id]
    if venues_by_id:
        conditions.append(
            (AuditLog.target_type == "venue_account") & AuditLog.target_id.in_(list(venues_by_id))
        )
    # Every sync decrypts, so a busy account writes a decrypt row every few
    # minutes: one query for the rest and a short one for decrypts keeps a
    # sign-in or a password change from being pushed off the list by them.
    newest = (AuditLog.at.desc(), AuditLog.id.desc())
    decrypt = AuditLog.action == AuditAction.CREDENTIAL_DECRYPT
    others = db.scalars(
        select(AuditLog).where(or_(*conditions), ~decrypt).order_by(*newest).limit(limit)
    ).all()
    decrypts = db.scalars(
        select(AuditLog)
        .where(or_(*conditions), decrypt)
        .order_by(*newest)
        .limit(min(limit, AUDIT_TRAIL_DECRYPTS))
    ).all()
    rows = sorted([*others, *decrypts], key=lambda r: (sessions.aware(r.at), r.id), reverse=True)[
        :limit
    ]
    out = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if row.target_type == "venue_account":
            venue = venues_by_id.get(row.target_id or "")
        elif row.target_type == "venue":
            venue = row.target_id
        else:
            venue = details.get("venue")
        venue = str(venue) if venue else ""
        purpose = next((str(details[k]) for k in _PURPOSE_KEYS if details.get(k)), "")
        out.append(
            {
                "action": row.action,
                "label": AUDIT_LABELS.get(row.action, row.action),
                "purpose": purpose.replace("_", " ")[:60],
                "venue": PROFILES[venue].display_name if venue in PROFILES else venue[:40],
                "at": sessions.aware(row.at),
                "ip": row.ip or ("Koine worker" if row.actor_user_id is None else "unknown"),
            }
        )
    return out


def _page(request: Request, db: Session, user: User, status_code: int = 200, **extra):
    resp = _render(request, "account_security.html", **_security_ctx(request, db, user, **extra))
    resp.status_code = status_code
    return resp


def _need_user(request: Request, db: Session) -> User | None:
    return optional_user(request, db)


@router.get("/dashboard/security", response_class=HTMLResponse)
def security_page(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _page(request, db, user)


@router.post("/dashboard/security/totp/enroll", response_class=HTMLResponse)
def totp_enroll(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        secret = mfa.start_enrolment(db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _enroll_page(request, user, secret)


def _enroll_page(request: Request, user: User, secret: str, error: str | None = None, code=200):
    uri = mfa.provisioning_uri(secret, user.email)
    resp = _render(
        request,
        "totp_enroll.html",
        user=user,
        qr=mfa.qr_svg(uri),
        secret=" ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
        error=error,
    )
    resp.status_code = code
    return resp


@router.post("/dashboard/security/totp/confirm", response_class=HTMLResponse)
def totp_confirm(request: Request, code: str = Form(), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        codes = mfa.confirm_enrolment(request, db, user, code)
    except HTTPException as exc:
        secret = mfa.pending_secret(db, user)
        if secret is None:
            return _page(request, db, user, _status(exc), error=exc.detail)
        return _enroll_page(request, user, secret, str(exc.detail), _status(exc))
    return _render(request, "recovery_codes.html", user=user, codes=codes, fresh=True)


@router.post("/dashboard/security/step-up")
def step_up_form(
    request: Request,
    code: str = Form(),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        mfa.step_up(request, db, user, code)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/dashboard/security/totp/disable")
def totp_disable_form(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if code:
            mfa.step_up(request, db, user, code)
        mfa.ensure_step_up(request, db, user)
        mfa.disable(request, db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _page(request, db, user, notice="Two-factor authentication is off.")


@router.post("/dashboard/security/recovery-codes", response_class=HTMLResponse)
def recovery_codes_form(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if code:
            mfa.step_up(request, db, user, code)
        mfa.ensure_step_up(request, db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    codes = mfa.replace_recovery_codes(db, user)
    return _render(request, "recovery_codes.html", user=user, codes=codes, fresh=False)


@router.post("/dashboard/security/sessions/revoke-all")
def revoke_all_form(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is not None:
        account.cancel_email_changes(db, user.id)  # committed by revoke_all
        sessions.revoke_all(db, user.id)
    sessions.revoke_current(db, request)
    return RedirectResponse("/login", status_code=303)


@router.post("/dashboard/security/sessions/{session_id}/revoke")
def revoke_one_form(request: Request, session_id: int, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    sessions.revoke_one(db, user.id, session_id)
    return RedirectResponse("/dashboard/security", status_code=303)


@router.post("/dashboard/security/verify-email/resend")
def resend_form(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.email_verified_at is None:
        try:
            ratelimit.check(request, "verify_resend_user", user.id)
        except HTTPException as exc:
            return _page(request, db, user, _status(exc), error=exc.detail)
        account.send_verification_email(db, user)
    return _page(request, db, user, notice=f"We sent a new link to {user.email}.")


@router.post("/dashboard/security/password")
def change_password_form(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    db: Session = Depends(get_db),
):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if not 10 <= len(new_password) <= 128:
            raise HTTPException(status_code=422, detail="Use 10 to 128 characters.")
        account.change_password(request, db, user, current_password, new_password)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _page(request, db, user, notice="Password changed. Other sessions were signed out.")


# ---- email verification, forgot / reset password ----------------------------------


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = account.verify_email(db, token) if token else None
    return _render(
        request,
        "account_notice.html",
        user=optional_user(request, db),
        title="Email confirmed" if user else "Link expired",
        heading="Email confirmed." if user else "This link has expired.",
        body=(
            "Thanks. You can now connect a venue once two-factor authentication is on."
            if user
            else "Verification links work once and last 48 hours. "
            "Send a new one from your security settings."
        ),
        cta=("/dashboard/security", "Security settings"),
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_page(request: Request):
    return _render(request, "password_reset.html", user=None, mode="request", error=None)


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_form(request: Request, email: str = Form(), db: Session = Depends(get_db)):
    try:
        account.request_password_reset(request, db, email.strip())
    except HTTPException as exc:
        resp = _render(request, "password_reset.html", user=None, mode="request", error=exc.detail)
        resp.status_code = _status(exc)
        return resp
    return _render(
        request,
        "account_notice.html",
        user=None,
        title="Check your email",
        heading="Check your email.",
        body=account.RESET_REQUESTED + " The link lasts 1 hour.",
        cta=("/login", "Back to log in"),
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_page(request: Request, token: str = ""):
    return _render(request, "password_reset.html", user=None, mode="reset", token=token, error=None)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_form(
    request: Request,
    token: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        if not 10 <= len(password) <= 128:
            raise HTTPException(status_code=422, detail="Use 10 to 128 characters.")
        account.reset_password(db, token, password)
    except HTTPException as exc:
        resp = _render(
            request, "password_reset.html", user=None, mode="reset", token=token, error=exc.detail
        )
        resp.status_code = _status(exc)
        return resp
    sessions.revoke_current(db, request)
    return _render(
        request,
        "account_notice.html",
        user=None,
        title="Password changed",
        heading="Password changed.",
        body="Every session was signed out. Log in with your new password.",
        cta=("/login", "Log in"),
    )


# ---- wave2:account-security: email change ---------------------------------------------


@router.post("/dashboard/security/email")
def change_email_form(
    request: Request,
    new_email: str = Form(),
    current_password: str = Form(),
    code: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if "@" not in new_email or len(new_email) > 254:
            raise HTTPException(status_code=422, detail="Enter a valid email address.")
        if code:
            mfa.step_up(request, db, user, code)
        account.request_email_change(request, db, user, current_password, new_email)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _page(request, db, user, notice=account.EMAIL_CHANGE_REQUESTED)


@router.get("/account/email/confirm", response_class=HTMLResponse)
def confirm_email_change_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = account.confirm_email_change(request, db, token) if token else None
    return _render(
        request,
        "account_notice.html",
        user=optional_user(request, db),
        title="Email changed" if user else "Link not valid",
        heading="Email changed." if user else "This link cannot be used.",
        body=(
            f"Your Koine email address is now {user.email}. Every other session was "
            "signed out, and we told your previous address."
            if user
            else account.EMAIL_CHANGE_FAILED
            + " Links work once and last 1 hour. Request a new one from your security settings."
        ),
        cta=("/dashboard/security", "Security settings"),
    )
